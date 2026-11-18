#!/usr/bin/env bash
# PostgreSQL 比較（ADR 0045 完了条件 5）。POSTGRES_USER / POSTGRES_DB / POSTGRES_PASSWORD / POSTGRES_PORT が必要（無ければ失敗）。
# 評価器とネイティブの両方で同じ操作列を PostgreSQL と比べる。CI は隔離網の PostgreSQL 公式イメージへ nsenter して実行する。
set -euo pipefail
: "${SIGIL:?SIGIL=/path/to/sigil を指定してください}"
: "${POSTGRES_USER:?}" "${POSTGRES_DB:?}" "${POSTGRES_PASSWORD:?}" "${POSTGRES_PORT:?}"
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
echo "=== PostgreSQL 比較: SIR 評価器 ==="
OUT="$($SIGIL run --root . pg_runner.sigil main)"
echo "$OUT"
echo "$OUT" | tail -1 | grep -qx "0"
echo "=== PostgreSQL 比較: ネイティブ ==="
BIN="$(mktemp -t sigil_db_pg_XXXXXX)"
$SIGIL build --root . pg_runner.sigil main -o "$BIN"
"$BIN"
rm -f "$BIN"
echo "=== PostgreSQL 比較 合格 ==="
