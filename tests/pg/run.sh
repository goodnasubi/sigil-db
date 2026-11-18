#!/usr/bin/env bash
# PostgreSQL 比較:
#   1. ADR 0045 完了条件 5: 保存層と公式 PostgreSQL の比較 (pg_runner.sigil)
#   2. ADR 0046 完了条件 2: サーバーと公式 PostgreSQL の差分比較 (diff_compare.py)
# POSTGRES_USER / POSTGRES_DB / POSTGRES_PASSWORD / POSTGRES_PORT が必要（無ければ失敗）。
# CI は隔離網の PostgreSQL 公式イメージへ nsenter して実行する。
set -euo pipefail
: "${SIGIL:?SIGIL=/path/to/sigil を指定してください}"
: "${POSTGRES_USER:?}" "${POSTGRES_DB:?}" "${POSTGRES_PASSWORD:?}" "${POSTGRES_PORT:?}"
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

echo "=== 1. PostgreSQL 保存層比較 (ADR 0045 完了条件 5): SIR 評価器 ==="
OUT="$($SIGIL run --root . pg_runner.sigil main)"
echo "$OUT"
echo "$OUT" | tail -1 | grep -qx "0"

echo "=== 2. PostgreSQL 保存層比較 (ADR 0045 完了条件 5): ネイティブ ==="
BIN="$(mktemp -t sigil_db_pg_XXXXXX)"
$SIGIL build --root . pg_runner.sigil main -o "$BIN"
"$BIN"
rm -f "$BIN"
echo "=== PostgreSQL 保存層比較 合格 ==="

echo "=== 3. PostgreSQL サーバー差分比較 (ADR 0046 完了条件 2): 公式 PG vs 本サーバー ==="
DIFF_DIR="$(mktemp -d -t sigil_db_diff_XXXXXX)"
cleanup() {
  if [ -n "${SERVER_PID:-}" ]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$DIFF_DIR"
}
trap cleanup EXIT

SERVER_BIN="$DIFF_DIR/server"
$SIGIL build --root . pg_server_runner.sigil main -o "$SERVER_BIN"

SIGIL_DB_DIR="$DIFF_DIR/db" \
SIGIL_DB_USER="wire" \
SIGIL_DB_NAME="wire" \
SIGIL_DB_PASSWORD="temporary test credential" \
SIGIL_DB_PORT="0" \
SIGIL_DB_HOST="127.0.0.1" \
SIGIL_DB_WAIT_MS="500" \
"$SERVER_BIN" > "$DIFF_DIR/server.log" 2>&1 &
SERVER_PID=$!

# ポート番号が server.log に出力されるのを待つ (最大 10 秒)
SIGIL_PORT=""
for _ in $(seq 1 100); do
  if [ -s "$DIFF_DIR/server.log" ]; then
    SIGIL_PORT="$(head -n 1 "$DIFF_DIR/server.log" | tr -dc '0-9')"
    if [ -n "$SIGIL_PORT" ] && [ "$SIGIL_PORT" -gt 0 ]; then
      break
    fi
  fi
  sleep 0.1
done

if [ -z "$SIGIL_PORT" ] || [ "$SIGIL_PORT" -le 0 ]; then
  echo "Error: Sigil DB server did not output listening port. Log:" >&2
  cat "$DIFF_DIR/server.log" >&2
  exit 1
fi

echo "Sigil DB server listening on 127.0.0.1:$SIGIL_PORT (PID: $SERVER_PID)"

# 差分比較の実行
python3 tests/pg_server/diff_compare.py \
  "$SIGIL_PORT" 127.0.0.1 \
  "$POSTGRES_PORT" "${POSTGRES_HOST:-127.0.0.1}" \
  "$POSTGRES_USER" "$POSTGRES_DB" "$POSTGRES_PASSWORD"

echo "=== PostgreSQL サーバー差分比較 (ADR 0046 完了条件 2) 合格 ==="
