# 版管理と互換 (ADR 0045 決定 4)

## 方針

- 保存形式の版は 1 つの整数（現在 `1`。`db/compat.sigil` の `current_format_version`）。ページ・WAL・checkpoint メタデータ・バックアップ manifest・完了印はどれも同じ版の欄を持つ。
- **新しい reader は、対応済みのすべての旧版の保存データとバックアップを読めなければならない**（`oldest_readable_format_version`〜`current_format_version`）。
- **未知の版は拒否する**（`UnsupportedFormat(版)`）。古い reader が新しい版を読もうとしたときも同じ。検査値が正しくても通さない。
- **インプレースの自動移行はしない。** `open_database` は版を書き換えない。旧版から新版へは `migrate_database(src, dst, db_id)` による**コピーでの明示移行**だけで行う。
- 移行は、原本を通常の open で読み、途中経過のバックアップ（`dst.migrating`）を経て、全件検証つきで `dst`（新しい空ディレクトリ）へ復元する。
  - `dst` または途中経過ディレクトリが既にあれば `Exists`。何も変えない。
  - 原本を開けない（未知の版・破損）場合は、何も作らずに失敗する。
  - 途中で失敗しても、原本はそのまま開ける。原本の論理状態は変わらない（open の復旧と checkpoint はコミット済みデータを変えない）。途中経過ディレクトリは掃除する。
- **互換を壊す変更**（版を上げる変更）には、ADR、新しい版の保存 fixture、旧版 fixture を読む試験、`migrate_database` の旧版→新版の経路が必要。旧版の fixture は消さない。

## 保存 fixture

`tests/fixtures/<版>/` に、その版の実装が書いたバイト列そのものを保存する。

| ディレクトリ | 内容 |
|---|---|
| `tests/fixtures/v1/db/` | DB ディレクトリ。commit 1 は checkpoint 済み、commit 2 は WAL にだけ残っている（open で復旧される） |
| `tests/fixtures/v1/backup/` | 上と同じ論理状態のバックアップ（完了印つき） |

論理状態の期待値は `tests/v1_state.sigil` に固定してある（生成器とは独立）。試験 (`tests/test_compat.sigil`) は次を確かめる。

1. 保存した DB を新しい reader が開き、WAL の復旧を経て期待どおりの論理状態になる
2. 保存したバックアップを検証し、復元した DB が同じ論理状態になる
3. 未知の版（page 0 の版を 2 にして検査値を作り直したもの）は、open も移行も `UnsupportedFormat`。移行先は作られない
4. コピー移行が成功し、原本も同じ状態で開ける。移行先が既にあれば `Exists` で、原本・既存の移行先は変わらない
5. **再現性**: 現在のコードが `tests/fixture_gen.sigil` で生成するバイト列が、保存 fixture と一致する。形式が意図せず変わると、ここで落ちる（形式の漂流の検出）

fixture を作り直すのは形式を意図して変えるときだけで、その場合は版を上げて新しいディレクトリを足す。再生成は `tests/fixture_gen.sigil` の `generate_v1_db` と `generate_v1_backup` を呼ぶ一時プログラムで行う。
