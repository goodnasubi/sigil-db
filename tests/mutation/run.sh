#!/usr/bin/env bash
# 変異試験: 保存層に意図した誤りを 1 つずつ埋め、試験が落ちることを確かめる。
# 検出されない変異（UNDETECTED）があれば終了コード 1。結果の表は docs/VERIFICATION.md に記録する。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
: "${SIGIL:?SIGIL=/path/to/sigil を指定してください}"
exec python3 "$SCRIPT_DIR/tests/mutation/mutations.py" "$SCRIPT_DIR" "$SIGIL" "$@"
