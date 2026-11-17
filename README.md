# sigil-db

Sigil で書かれた組込みデータベース。固定ページと B+tree、WAL による ACID な保存核（`goodnasubi/sigil` の P4 段階、ADR 0044・ADR 0045）。

## 概要

本リポジトリは Sigil の第 4 段階（P4 組込みデータベース）の実装です。
ADR 0045（決定 1・決定 2）に従い、保存形式 v1 と上限値の純粋な核を提供します。

### モジュール構成 (`db/`)

- `db/limits.sigil`: ADR 0045 決定 2 に基づくシステム上限値定数（ページ数 2^24、行 4096B、キー 1024B、列数 64 等）と境界検査関数
- `db/error.sigil`: データベース核のエラー型（`DbError`: `Limit`, `UnsupportedFormat`, `Corrupt`, `TypeMismatch`）
- `db/format.sigil`: 共通 96 バイトヘッダ（マジックバイト、バージョン、リトルエンディアン整数、SHA-256 検査値）の符号化・復号・検証
- `db/types.sigil`: 値型（`Int`, `Bool`, `Str`, `Bytes`）の符号化・大小順序比較、行およびキーの符号化・復号
- `db/page.sigil`: 8192 バイト固定長スロットページ構造、アイテム挿入・取得、空き容量計算、整合性検証
- `db/wal.sigil`: WAL レコード構造（Begin, Redo 全ページ像, Commit, Abort, Checkpoint）の生成・検証
- `db/fixture.sigil`: 正例（正常なメタページ・スロットページ・WAL レコード）および負例（検査値不一致、未知バージョン、破損、各種上限超過）の fixture 群

### 仕様書

- [docs/FORMAT_V1.md](docs/FORMAT_V1.md): 保存形式 v1 のバイナリレイアウトおよび上限値の仕様

## テストの実行

### 前提条件

Sigil コンパイラ（PR goodnasubi/sigil#594 のマージコミット `d78779aec924bfd2207ec12a3712e211b6b76ccf` を含む固定版）が必要です。

### テスト実行

付属の `test.sh` スクリプトで整形検査・型検査・SIR 評価器実行・ネイティブバイナリビルド＆実行を一括して確認できます。

```bash
./test.sh
```

個別に実行する場合:

```bash
# 整形検査
sigil fmt --check db/*.sigil tests/*.sigil test_runner.sigil

# 型検査
sigil check --root . test_runner.sigil

# SIR 評価器での実行
sigil run --root . test_runner.sigil main

# ネイティブバイナリのビルドと実行
sigil build --root . test_runner.sigil main -o run_tests && ./run_tests
```
