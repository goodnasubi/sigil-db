# P4-3 へ渡す共通規則 (ADR 0045 決定 4)

P4-3（リポジトリの核）が共有する規則を、この DB 核での実現位置と試験とともに記録する。**Git オブジェクトや参照に、DB のページ形式や WAL を流用することは強制しない。** 規則は「永続化の作法」であり、形式ではない。

| # | 規則 | この DB 核での実現 | 試験 |
|---|---|---|---|
| 1 | **更新の公開点を一つにする**。読み手が見る状態は、公開点の前後でだけ変わる | commit は WAL の commit レコード（同期済み）、checkpoint は `checkpoint.meta` の rename、バックアップは `backup.complete` の rename | `tests/test_wal.sigil`、`tests/test_backup.sigil` |
| 2 | **同期前に成功を返さない**。ファイル同期（`sync_all`）と、作成・rename した名前を持つ親ディレクトリの同期を終えてから成功にする | `tx_commit`（WAL 同期後）、`checkpoint_database`（各段階）、`open_database`（新規 DB と wal.log の親同期）、`backup_database`（ファイル、manifest、完了印、親ディレクトリ） | `tests/test_wal.sigil`、`tests/test_carryover.sigil` |
| 3 | **同一 FS の rename、親ディレクトリ同期**。置換は同一ディレクトリ内の一時名への書込み→同期→rename→ディレクトリ同期。別 FS は失敗にし、コピーして消す代替はしない | `checkpoint.meta.tmp`→`checkpoint.meta`、`wal.log.tmp`→`wal.log`、`backup.complete.tmp`→`backup.complete` | 同上 |
| 4 | **長さ・版・検査値を使う前に検証する**。宣言された長さだけで割り当てない。版が未知なら拒否する | 共通ヘッダ（magic・版・長さ・SHA-256）、スロットの敷き詰め、上限の割当て前検査、manifest の構造検査、バックアップの全ページ検証 | `tests/test_format.sigil`、`tests/test_limits.sigil`、`tests/test_backup.sigil` |
| 5 | **復旧に必要な旧データは、公開が完了するまで保持する**。古い WAL・旧ファイルは、新しい状態の公開と親ディレクトリ同期が済んでから回収する | checkpoint の 5 手順（WAL の切詰めは `checkpoint.meta` 公開の後）、バックアップは完了印の前に宛先を「未完成」として扱う、移行は原本を変えず途中経過を別ディレクトリに置く | `tests/test_wal.sigil`、`tests/test_carryover.sigil`、`tests/test_compat.sigil` |
| 6 | **復旧は冪等**。何度途中で止まって再実行しても同じ状態になる | WAL 再生は LSN と commit レコードの有無で決まり、再 open を繰り返しても同じ | `tests/test_wal.sigil`（復旧）、`tests/test_compat.sigil`（保存 WAL の open を 2 回） |
| 7 | **障害注入と互換 fixture**。破損・切断・版違いの入力、旧版の保存物を試験に持つ | 改ざん・切断・余計なファイル・未知版の拒否、版ごとの保存 fixture と再現性の試験 | `tests/test_backup.sigil`、`tests/test_compat.sigil`。本格的な障害注入と変異試験は第 5 段（検証記録） |
| 8 | **バックアップは停止時間を許し、稼働中の任意ファイルコピーをバックアップと呼ばない** | 新 tx を止め、checkpoint 後に排他ロックを保ってコピーし、完了印のあるものだけを読む | `tests/test_backup.sigil` |
| 9 | **互換を壊す変更は ADR とコピーによる明示移行を必要とし、インプレースの自動移行はしない** | `db/compat.sigil`、`docs/COMPAT.md` | `tests/test_compat.sigil` |

## P4-3 が選べること・選ばなくてよいこと

- 規則 1〜6 は形式に依らない。Git の loose object や ref の更新も「一時名に書く→同期→同一 FS rename→親ディレクトリ同期」で公開点を一つにできる。
- DB のページ形式・B+tree・WAL を使うかどうかは P4-3 が決める。使わない場合も、規則 4（使う前に検証）と 7（互換 fixture）は同様に満たす。
- SHA-1 が必要かどうかは P4-3 が決める（本 ADR の P4-1 は SHA-256 だけを使う）。
