#!/usr/bin/env bash
set -euo pipefail

# Sigil binary resolution
if [ -z "${SIGIL:-}" ]; then
  if command -v sigil >/dev/null 2>&1; then
    SIGIL="sigil"
  else
    echo "Error: sigil binary not found on PATH. Set SIGIL=/path/to/sigil or install it to PATH." >&2
    exit 1
  fi
fi

echo "Using Sigil compiler: $SIGIL"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== 1. Checking format (sigil fmt --check) ==="
$SIGIL fmt --check db/*.sigil tests/*.sigil test_runner.sigil mutation_runner.sigil pg_runner.sigil xcheck_runner.sigil

echo "=== 2. Type checking (sigil check) ==="
$SIGIL check --root . test_runner.sigil
for entry in mutation_runner.sigil pg_runner.sigil xcheck_runner.sigil; do
  $SIGIL check --root . "$entry"
done

echo "=== 3. Running tests via SIR evaluator (sigil run) ==="
# 検証記録のため、テストは件数などを標準出力へ書く。合否は最後の行（main の戻り値）が 0 かで決める。
RUN_OUTPUT="$($SIGIL run --root . test_runner.sigil main)"
echo "$RUN_OUTPUT"
if [ "$(printf '%s\n' "$RUN_OUTPUT" | tail -n 1)" != "0" ]; then
  echo "Test failure in SIR evaluator: expected last line 0" >&2
  exit 1
fi
echo "SIR evaluator tests passed (result = 0)"

echo "=== 4. Building and running native binary (sigil build) ==="
TMP_BIN="$(mktemp -t sigil_db_test_XXXXXX)"
$SIGIL build --root . test_runner.sigil main -o "$TMP_BIN"
"$TMP_BIN"
rm -f "$TMP_BIN"
echo "Native binary tests passed"

echo "=== 5. ネイティブと SIR 評価器の突き合わせ (ADR 0045 完了条件 7) ==="
tests/xcheck/run.sh

# 追加の検証（時間がかかるので環境変数で選ぶ。CI は両方を有効にする）
if [ "${SIGIL_DB_MUTATION:-0}" = "1" ]; then
  echo "=== 6. 変異試験（保存層の注入器。検出されない変異は期待と照合） ==="
  tests/mutation/run.sh
fi
if [ "${SIGIL_DB_PERF:-0}" = "1" ]; then
  echo "=== 7. 性能の記録（合否に使わない） ==="
  tests/perf/run.sh
fi

echo "=== All tests passed successfully! ==="
