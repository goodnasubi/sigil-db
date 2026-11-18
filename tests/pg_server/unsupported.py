"""ADR 0046 完了条件 3: 未対応機能の安全性検証

CancelRequest, Flush 単独, FunctionCall, COPY 等の未対応プロトコルメッセージ、
および NULL 値、外部結合、サブクエリ、SHOW、ALTER TABLE、TRUNCATE などの
未対応 SQL を投入したときに、暗黙の誤動作やサーバー panic を起こさず、
明示的エラー（FeatureNotSupported / SQLSTATE 0A000 / 42601 等）で拒否され、
セッション状態が安全に維持または終了することを検証する。
"""

import socket
import struct
import sys
from contextlib import closing

from loopback import PASSWORD, connect, exact, frame, query, receive, sqlstate, u32, until_ready


def test_unsupported_protocol_messages(port, host):
    print("  - Testing unsupported protocol messages (CancelRequest, Flush, FunctionCall, COPY)...")
    # 1. CancelRequest (Code 80877102): ログ記録後、接続を切断
    with closing(socket.create_connection((host, port), timeout=5)) as sock:
        cancel_packet = u32(80877102) + u32(1234) + u32(5678)
        sock.sendall(u32(len(cancel_packet) + 4) + cancel_packet)
        # サーバーは明示的エラー (0A000) を返して安全に切断する
        reply = receive(sock)
        sqlstate(reply, b"0A000")
        assert sock.recv(1) == b"", "CancelRequest should cause safe connection close"

    # 2. Flush 単独 ('H')
    with closing(connect(port, host)) as sock:
        sock.sendall(frame(b"H") + frame(b"S"))
        replies = until_ready(sock)
        sqlstate(replies[0], b"0A000")
        # エラー後もセッションが維持されること（ReadyForQuery が返り、後続クエリが通る）
        assert replies[-1] == (b"Z", b"I")
        query(sock, "SELECT id FROM wire_rows")

    # 3. FunctionCall ('F')
    with closing(connect(port, host)) as sock:
        sock.sendall(frame(b"F", u32(0) + struct.pack("!H", 0) + struct.pack("!H", 0)) + frame(b"S"))
        replies = until_ready(sock)
        sqlstate(replies[0], b"0A000")
        assert replies[-1] == (b"Z", b"I")
        query(sock, "SELECT id FROM wire_rows")

    # 4. COPY 関連 ('d', 'c', 'f')
    for tag in (b"d", b"c", b"f"):
        with closing(connect(port, host)) as sock:
            sock.sendall(frame(tag, b"copy payload") + frame(b"S"))
            replies = until_ready(sock)
            sqlstate(replies[0], b"0A000")
            assert replies[-1] == (b"Z", b"I")
            query(sock, "SELECT id FROM wire_rows")


def test_unsupported_sql_features(port, host):
    print("  - Testing unsupported SQL features (NULL, SHOW, functions, OUTER JOIN, subqueries, etc.)...")
    with closing(connect(port, host)) as sock:
        query(sock, "CREATE TABLE unsup_t (id BIGINT PRIMARY KEY, v TEXT)")

        # 1. NULL 値の投入
        err = query(sock, "INSERT INTO unsup_t VALUES (1, NULL)")[0]
        assert err[0] == b"E", "NULL value must be rejected with explicit error"

        # 2. SHOW 変数照会
        for var in ("application_name", "bytea_output", "server_version"):
            err = query(sock, f"SHOW {var}")[0]
            assert err[0] == b"E", f"SHOW {var} must be rejected with explicit error"

        # 3. システム情報関数
        for fn in ("pg_backend_pid()", "version()", "current_database()"):
            err = query(sock, f"SELECT {fn}")[0]
            assert err[0] == b"E", f"Function {fn} must be rejected with explicit error"

        # 4. OUTER JOIN (LEFT / RIGHT / FULL)
        query(sock, "CREATE TABLE unsup_t2 (id BIGINT PRIMARY KEY, t_id BIGINT)")
        for join_kw in ("LEFT JOIN", "RIGHT JOIN", "FULL JOIN"):
            err = query(sock, f"SELECT * FROM unsup_t {join_kw} unsup_t2 ON unsup_t.id = unsup_t2.t_id")[0]
            assert err[0] == b"E", f"{join_kw} must be rejected with explicit error"

        # 5. サブクエリ / HAVING / WINDOW
        err = query(sock, "SELECT * FROM (SELECT id FROM unsup_t) AS sub")[0]
        assert err[0] == b"E", "Subquery must be rejected with explicit error"

        err = query(sock, "SELECT COUNT(*) FROM unsup_t GROUP BY id HAVING COUNT(*) > 1")[0]
        assert err[0] == b"E", "HAVING must be rejected with explicit error"

        # 6. UNION / INTERSECT / EXCEPT
        err = query(sock, "SELECT id FROM unsup_t UNION SELECT id FROM unsup_t2")[0]
        assert err[0] == b"E", "UNION must be rejected with explicit error"

        # 7. DDL: ALTER TABLE / TRUNCATE
        err = query(sock, "ALTER TABLE unsup_t ADD COLUMN extra INT")[0]
        assert err[0] == b"E", "ALTER TABLE must be rejected with explicit error"

        err = query(sock, "TRUNCATE unsup_t")[0]
        assert err[0] == b"E", "TRUNCATE must be rejected with explicit error"

        # 8. 複数文 (セミコロン区切り)
        err = query(sock, "SELECT 1; SELECT 2")[0]
        sqlstate(err, b"42601")

        # 9. トランザクション指定の未対応: SAVEPOINT
        err = query(sock, "SAVEPOINT sp1")[0]
        assert err[0] == b"E", "SAVEPOINT must be rejected with explicit error"

        # 10. DISCARD の未対応構文: PLANS, TEMP, SEQUENCES, RESET ALL
        for d in ("DISCARD PLANS", "DISCARD TEMP", "DISCARD SEQUENCES", "RESET ALL"):
            err = query(sock, d)[0]
            assert err[0] == b"E", f"{d} must be rejected with explicit error"

        # すべてのエラー後もセッションが健全で後続クエリが動作すること
        res = query(sock, "SELECT id FROM unsup_t2")
        assert len(res) >= 2 and res[-1][0] == b"C"

        query(sock, "DROP TABLE unsup_t")
        query(sock, "DROP TABLE unsup_t2")


def main(port, host="127.0.0.1"):
    print(f"=== Condition 3: Unsupported features safety verification ({host}:{port}) ===")
    test_unsupported_protocol_messages(port, host)
    test_unsupported_sql_features(port, host)
    print("=== Condition 3 passed: All unsupported features rejected safely without panic ===")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 unsupported.py <port> [host]")
        sys.exit(1)
    port = int(sys.argv[1])
    host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    main(port, host)
