#!/usr/bin/env bash
# ネイティブと SIR 評価器の突き合わせ（ADR 0045 完了条件 7）: 同じ操作列・エラー・上限のトレースのダイジェストが一致すること。
set -euo pipefail
: "${SIGIL:?SIGIL=/path/to/sigil を指定してください}"
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
EVAL_OUT="$($SIGIL run --root . xcheck_runner.sigil digest | head -1)"
BIN="$(mktemp -t sigil_db_xc_XXXXXX)"
$SIGIL build --root . xcheck_runner.sigil digest -o "$BIN"
NATIVE_OUT="$("$BIN" | head -1)"
rm -f "$BIN"
echo "evaluator digest=$EVAL_OUT native digest=$NATIVE_OUT"
if [ "$EVAL_OUT" = "FAILED" ] || [ "$EVAL_OUT" != "$NATIVE_OUT" ]; then
  echo "評価器とネイティブのトレースが一致しない" >&2
  exit 1
fi
