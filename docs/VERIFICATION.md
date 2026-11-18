# 検証記録（P4-1 第5段 / ADR 0045 完了条件 1〜7）

固定コンパイラ: sigil `d78779a`。測定環境: Linux x86_64、Intel Xeon 2.10GHz 4 コア（クラウドコンテナ）。
所要時間はネイティブ実行のおおよその値。再現手順は各節の末尾に書く。

## 全体の実行入口

| 入口 | 内容 |
|---|---|
| `./test.sh` | fmt --check、check、評価器実行（標準出力が `0`）、ネイティブ build+実行、xcheck（SIR 評価器とネイティブの照合） |
| `SIGIL_DB_MUTATION=1 ./test.sh` | 上に加えて変異の注入（`tests/mutation/run.sh`） |
| `SIGIL_DB_PERF=1 ./test.sh` | 上に加えて性能記録（`tests/perf/run.sh`、合否には使わない） |
| `tests/pg/run.sh` | PostgreSQL 比較。`POSTGRES_USER/DB/PASSWORD/PORT` が必須。CI は公式イメージを使う |

## 注入点（記憶層の injector）

`db/iohook.sigil` の `io_gate` が、WAL の書込・同期、本体の書込・同期、メタの書込・同期、rename、ディレクトリ同期、WAL の切詰め・切詰め後の同期の前に呼ばれる。
動作は 0 通常、1 EIO、2 ENOSPC、3 torn(n)、4 crash-before、5 short(n)、6 再試行（EINTR 相当）、7 dead。実際に行われた副作用だけを事象として記録する（torn は先頭 n byte のみ）。
`Database.io` に状態を持つため、通常運用への影響は分岐 1 回のみ。

## 条件 1: ACID の性質テスト

`tests/test_acid.sigil`。seed `1` / `20261118` / `777`、各 20 tx（約 1/7 は意図した制約違反、約 1/6 は rollback）。
各 tx 後にモデル（`tests/workload.sigil` の純粋なモデル）と全行・二次索引走査の順序を照合。隔離（未 commit の書込みが他ハンドルから見えない）と、カタログ操作の原子性も検証する。約 18 秒。

## 条件 2: kill / 再 open と故障注入

`tests/test_crash.sigil`。持続性モデル（`tests/durable_model.sigil`）と実 FS を分けて記録する。

- **モデル駆動のクラッシュ像**: seed `20261118`、4 tx、主キー範囲 12、checkpoint を 2 tx ごと。記録した 30 事象の全境界で、keep / lose / torn(2 種の seed) の 4 方式で像を作り 計 124 像を再 open。約 26 秒。
- **生存中の故障注入**: 全 op 種別 × 動作（EIO、ENOSPC、torn、short、再試行、crash-before、dead）で 39 回（各 op 種別の最初の発生と WAL 追記の最後の発生）。約 100 秒。同期失敗後はハンドルが無効化される（`CommitUnknown`）。
- **不変条件**: 再 open は常に成功し、状態は ack 済みの最後の状態か、実行中だった tx が反映された状態のどちらか。復旧後も続けて書ける。

**未実施**: 実 FS の再起動（電源断）試験は行っていない。プロセス kill と上のモデルによる像で代替しており、実デバイスの持続性は検証していない。

## 条件 3: ファジング

`tests/test_fuzz.sigil`。seed `20261118`（決定的）。約 90 秒（回帰例含む）。

| 対象 | 回数 | 拒否 |
|---|---|---|
| main.db のランダム破壊 | 24 | 9 |
| wal.log のランダム破壊 | 24 | 13 |
| checkpoint.meta | 14 | 10 |
| ページ構造（検査値を計算し直す） | 20 | 5 |
| backup | 18 | 17 |

拒否されなかったものは、状態が許される状態と一致した場合のみ受理（それ以外は失敗）。
**回帰例（固定コーパス、29 件）**: 本体の全ページ × 4 か所の 1 byte 破壊、全ページの保存検査値の破壊（読まれるページで必ず拒否）、検査値を計算し直した WAL ページ像の偽造 3 件、tx 束の検査値だけを誤らせて自身の検査値を計算し直した commit レコード。
victim の WAL に commit 済みの tx が残っていることを表明で保証している（以前は空で WAL 側が空振りだったことを変異の注入で発見し、修正した）。

## 条件 4: 変異の注入（検出されなければ検証済みと数えない）

`tests/mutation/run.sh`（約 6 分）。各変異は 1 つの入口テストが失敗することで検出とする。

| 変異 | 結果 | 入口 |
|---|---|---|
| WAL の同期を省く | DETECTED | images |
| commit レコードの一部だけ書く（部分適用） | DETECTED | images |
| checkpoint が同期前に WAL を回収する | DETECTED | images |
| checkpoint が本体を同期しない | DETECTED | images |
| checkpoint メタを同期せず rename | DETECTED | images |
| checkpoint が最後のページを書かない | DETECTED | images |
| 同期失敗後にハンドルを無効化しない | DETECTED | live |
| ページ・WAL の SHA-256 を検証しない | DETECTED | fuzz |
| tx 束の検査値を検証しない | DETECTED | fuzz |
| 行の削除で二次索引を消さない | DETECTED | acid |
| rename 後のディレクトリ同期を省く | **UNDETECTED** | — |

最後の変異は検出されない。本体を meta より先に同期しており、meta は復旧に必須でないため、この省略では ack 済みの tx が失われない（等価に近い）。**検証済みとは数えない**。10/11 が検出。

## 条件 5: PostgreSQL との比較

`pg_runner.sigil` + `tests/pg_compare.sigil`、実行は `tests/pg/run.sh`。P1 の `pg` クライアントで、CI の PostgreSQL 公式イメージ
`postgres:17.6-bookworm@sha256:f3bd19c606e442c3d7bdfa8002e03fe260a1023351e0ea4598032022b68dd6e3` に対して比較する。
3 seed × 30 tx の同一操作列を両方へ流し、(a) 全行 `ORDER BY id`、(b) 二次索引の走査順 `(grp, id)`、(c) 一意索引の列（`COLLATE "C"`）、(d) 制約違反の種類（重複キー = UniqueViolation、存在しない行 = NotFound）を比較する。比較した制約違反は 34 件。評価器 約 25 秒、ネイティブ 約 13 秒。
ローカルでの確認は PostgreSQL 16 で行い、CI のイメージ（17.6）での実行は CI の結果で確認する。

## 条件 6: 新しい読み手と旧フィクスチャ・旧バックアップ

第 4 段で追加済み（`tests/test_compat.sigil`、`tests/v1_state.sigil`、`tests/fixtures/`）。本段で実装した読み手の変更はないため、同じテストが毎回の `test.sh` で通っていることを根拠とする。

## 条件 7（前段）: 評価器とネイティブの照合

`tests/xcheck.sigil` の `trace_digest` が、計画の結果・走査・過大な行の `Limit` エラーを 1 つの値に畳む。SIR 評価器とネイティブで一致することを `tests/xcheck/run.sh` が検査する（ローカルの値は `1850636933`）。
信頼差分の監査は dependent-fs（共通 FS 境界）の監査と対応する: goodnasubi/sigil `docs/decisions/0045-p4-1-database-core.md` の信頼境界の節。評価器とネイティブの差が出た場合はこの監査の項目に照らして切り分ける。

## 条件 7: 性能記録（合否に使わない）

`tests/perf/run.sh`。固定ワークロード: 1 行挿入の tx を 400 回連続 commit、WAL 追記ごとに fsync、終了後に再 open。

固定版 `d78779a`、装置は上記、件数はネイティブ 400・評価器 40、seed なし（固定列）、同期方式は WAL 追記ごとの fsync。

| 経路 | commit 数 | commit 全体の秒 | 再 open の秒 | 作業ページ数 (8KiB) |
|---|---|---|---|---|
| SIR 評価器 | 40 | 12 | 23 | 5 |
| ネイティブ | 400 | 69 | 126 | 15 |

commit あたりの遅延はネイティブ約 0.17 秒、評価器約 0.3 秒。**再 open（WAL の再生）は commit 数に対して重い**（400 commit で 126 秒）。ページごとの SHA-256 検査と tx 束の検査値の検証が支配的と見られる。
合否には使わず、最適化は範囲外（Issue 化の対象）。

評価器の時計は 1 秒単位（`clock.unix_seconds` のみ）なので粗い。

## 発見事項と制限

- **コンパイラの不具合（sigil 側）**: `if f(...)? {` のように `Result<Bool, _>` を条件に直接使うとネイティブで誤動作する。`let` に束縛して回避した。再現と報告は goodnasubi/sigil#602。
- **WAL の最後の tx の破損は torn tail と区別できない**（設計どおり）。未 commit として扱われ、メディア破損（クラッシュではない）では ack 済み tx が静かに失われうる。範囲外のため修正しない。
- **ディレクトリ同期の省略**は上のとおり検出不能（等価に近い）。
- 実 FS の再起動試験は未実施（上記）。
- 圧縮と SHA-1 は範囲外。
