#!/usr/bin/env python3
# 変異試験の定義と実行（ADR 0045「完了条件と後続の切り方」の注入器節）。
# 使い方: python3 tests/mutation/mutations.py <repo_root> <sigil> [name ...]
# 変異ごとに repo の複製へ意図した誤りを 1 つ埋め、決めた試験群（mutation_runner.sigil の入口）を実行する。
# 試験が落ちれば DETECTED、全て通れば UNDETECTED（検証済みとしない）。結果は表で出力する。
import os, shutil, subprocess, sys, tempfile, time

NOOP = '''
fn db_noop(database: &mut Database) -> Result<Bool, DbError>
  intent "変異試験用: 何もしないで成功する"
  requires none
  ensures none
  effects none
{
  return Ok(true);
}
'''

M = []
def mut(name, desc, path, old, new, entries, append="", expect="DETECTED", reason=""):
    M.append(dict(name=name, desc=desc, path=path, old=old, new=new, entries=entries, append=append, expect=expect, reason=reason))

DB = "db/database.sigil"
mut("wal_sync_omitted", "commit が WAL を同期せずに成功を返す", DB,
    'match db_wal_sync(&mut database) {\n            Err(_) => {\n              database.is_poisoned = true;\n              return Err(DbError.CommitUnknown("failed to sync WAL',
    'match db_noop(&mut database) {\n            Err(_) => {\n              database.is_poisoned = true;\n              return Err(DbError.CommitUnknown("failed to sync WAL',
    ["images"], NOOP)
mut("commit_partial", "commit レコードの末尾 20 byte を書かずに成功を返す（commit の部分適用）", DB,
    'match db_wal_write(&mut database, &all_wal_bytes) {',
    'match db_wal_write(&mut database, &all_wal_bytes.slice(0, all_wal_bytes.len() - 20).unwrap_or(Bytes.new())) {',
    ["images"])
mut("wal_deleted_early", "checkpoint が本体と meta の同期より前に WAL を回収する", DB,
    '  // (2) その全ページを本体へ位置指定書込みして本体を同期',
    '  let _ = db_wal_truncate(&mut database, &dir_p.join(&"wal.log"));\n  // (2) その全ページを本体へ位置指定書込みして本体を同期',
    ["images"])
mut("main_sync_omitted", "checkpoint が本体ファイルを同期せずに WAL を回収する", DB,
    'match db_main_sync(&mut database) {', 'match db_noop(&mut database) {', ["images"], NOOP)
mut("meta_sync_omitted", "checkpoint メタデータの一時ファイルを同期せずに rename する（持続性モデルでは未同期扱い）", DB,
    'let sg = io_gate(&mut database.io, op_meta_sync(), 0, &empty);',
    'let sg = io_gate(&mut database.io, op_meta_sync() * 0, 0, &empty);', ["images"])
mut("dir_sync_omitted_after_rename", "rename 後の親ディレクトリ同期を省いて WAL を回収する", DB,
    'match db_dir_sync(&mut database, &dir_p) {\n    Err(_) => {\n      database.is_poisoned = true;\n      return Err(DbError.Io("checkpoint failed at step 4',
    'match db_noop(&mut database) {\n    Err(_) => {\n      database.is_poisoned = true;\n      return Err(DbError.Io("checkpoint failed at step 4',
    ["images"], NOOP, expect="UNDETECTED",
    reason="検出されない。本体は meta より前に同期済みで、meta は復旧に必須でないため、この省略では ack 済みの tx が失われない（等価に近い変異）。検証済みと数えない")
mut("checkpoint_skips_last_page", "checkpoint が最後のページを本体へ書かない", DB,
    'while p_idx < database.committed_pages.len() {\n    let mut page_copy',
    'while p_idx + 1 < database.committed_pages.len() {\n    let mut page_copy', ["images"])
mut("sync_failure_not_poisoned", "WAL 同期の失敗後もハンドルを無効化しない", DB,
    'database.is_poisoned = true;\n              return Err(DbError.CommitUnknown("failed to sync WAL',
    'return Err(DbError.CommitUnknown("failed to sync WAL', ["live"])
mut("checksum_ignored", "ページ・WAL の SHA-256 検査値を検証しない", "db/format.sigil",
    'if !ct_eq(&stored_csum, &computed_csum) {', 'if false && !ct_eq(&stored_csum, &computed_csum) {', ["fuzz"])
mut("bundle_checksum_ignored", "tx 束の検査値を検証しない", "db/wal.sigil",
    'if !ct_eq(&computed, &expected_bundle_checksum) {', 'if false && !ct_eq(&computed, &expected_bundle_checksum) {', ["fuzz", "images"])
mut("index_entry_not_removed", "行の削除で二次索引の項目を消さない", "db/relation.sigil",
    'tree_delete(&database, &mut tx, ix.root, &index_key(&ov, &pk)?)?;\n    i = i + 1;',
    'i = i + 1;', ["acid"])

def sh(cmd, cwd, timeout):
    t = time.time()
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr, time.time() - t
    except subprocess.TimeoutExpired:
        return 124, "timeout", time.time() - t

def main():
    root, sigil = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
    only = set(sys.argv[3:])
    # 変異なしの基準: 全入口が通ること（落ちるなら変異試験は無意味）
    results = []
    for m in M:
        if only and m["name"] not in only:
            continue
        work = tempfile.mkdtemp(prefix="sigil_db_mut_")
        try:
            for d in ("db", "tests"):
                shutil.copytree(os.path.join(root, d), os.path.join(work, d), ignore=shutil.ignore_patterns("fixtures"))
            shutil.copy(os.path.join(root, "mutation_runner.sigil"), work)
            p = os.path.join(work, m["path"])
            s = open(p).read()
            if s.count(m["old"]) != 1:
                results.append((m["name"], m["desc"], "ERROR", "変異の位置が一意でない（%d 件）" % s.count(m["old"]), 0, m["expect"]))
                continue
            s = s.replace(m["old"], m["new"]) + m["append"]
            open(p, "w").write(s)
            status, note, total = "UNDETECTED", "", 0.0
            for e in m["entries"]:
                rc, out, _ = sh([sigil, "build", "--root", ".", "mutation_runner.sigil", e, "-o", "mut_bin"], work, 600)
                if rc != 0:
                    status, note = "ERROR", "build: " + out.strip()[:200]
                    break
                rc, out, dt = sh(["./mut_bin"], work, 900)
                total += dt
                if rc != 0 or out.strip().splitlines()[-1:] != ["0"]:
                    status, note = "DETECTED", "入口 %s が失敗" % e
                    break
            results.append((m["name"], m["desc"], status, note or m["reason"], total, m["expect"]))
        finally:
            shutil.rmtree(work, ignore_errors=True)
    print("| 変異 | 内容 | 結果 | 期待 | 備考 | 秒 |")
    print("|---|---|---|---|---|---|")
    bad = 0
    detected = 0
    for n, d, st, note, t, ex in results:
        print("| %s | %s | %s | %s | %s | %.0f |" % (n, d, st, ex, note, t))
        if st == "DETECTED":
            detected += 1
        if st != ex:
            bad += 1
    print()
    print("検出 %d / %d 件。検出されなかった変異は検証済みと数えない。期待と違う結果: %d 件" % (detected, len(results), bad))
    sys.exit(1 if bad else 0)

main()
