#!/usr/bin/env bash
# 性能の記録（ADR 0045 完了条件 7）。合否には使わない。固定版・装置・seed・件数・同期方式とともに両経路の値を出力する。
set -euo pipefail
: "${SIGIL:?SIGIL=/path/to/sigil を指定してください}"
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
echo "compiler: ${SIGIL_REF:-$($SIGIL --version 2>/dev/null || echo unknown)}"
echo "device: $(uname -srm); cpu: $(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2 | xargs || echo unknown); nproc=$(nproc 2>/dev/null || echo ?)"
echo "workload: 1 行挿入の tx を連続 commit（ネイティブ 400 回・評価器 40 回）（seed なし・固定列）、同期方式 = fsync（WAL 追記ごと）、全 commit 後に再 open"
echo "path commits commit_seconds recover_seconds working_pages(8KiB)"
EVAL="$($SIGIL run --root . xcheck_runner.sigil perf_small | head -4 | tr '\n' ' ')"
echo "evaluator $EVAL"
BIN="$(mktemp -t sigil_db_perf_XXXXXX)"
$SIGIL build --root . xcheck_runner.sigil perf -o "$BIN"
echo "native $("$BIN" | head -4 | tr '\n' ' ')"
rm -f "$BIN"
echo "(時計は 1 秒単位。commit 遅延 = commit_seconds / commits、throughput = commits / commit_seconds)"
