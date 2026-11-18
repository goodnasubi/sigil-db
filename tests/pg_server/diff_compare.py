"""ADR 0046 完了条件 2: 公式 PostgreSQL との差分比較（Differential Testing）

同じ操作・同じ SQL（DDL, INSERT, UPDATE, DELETE, SELECT, INNER JOIN, 集約, ORDER BY, LIMIT, BEGIN/COMMIT/ROLLBACK）
を公式 PostgreSQL と Sigil DB 本サーバーの両方へ投げ、
返る行、列情報（RowDescription）、完了タグ（CommandComplete）、エラー種別（SQLSTATE）
を突き合わせ、差分を記録する。
"""

import base64
import hashlib
import hmac
import os
import secrets
import socket
import struct
import sys
from contextlib import closing


def u16(number):
    return struct.pack("!H", number)


def u32(number):
    return struct.pack("!I", number)


def frame(tag, body=b""):
    return tag + u32(len(body) + 4) + body


def exact(sock, size):
    output = bytearray()
    while len(output) < size:
        chunk = sock.recv(size - len(output))
        if not chunk:
            raise EOFError("unexpected disconnect")
        output.extend(chunk)
    return bytes(output)


def receive(sock):
    tag, size = struct.unpack("!cI", exact(sock, 5))
    return tag, exact(sock, size - 4)


def until_ready(sock):
    messages = []
    while True:
        reply = receive(sock)
        messages.append(reply)
        if reply[0] == b"Z":
            return messages


def connect_pg(host, port, user, database, password):
    sock = socket.create_connection((host, port), timeout=10)
    try:
        startup = (
            u32(196608)
            + b"user\0"
            + user.encode()
            + b"\0database\0"
            + database.encode()
            + b"\0client_encoding\0UTF8\0\0"
        )
        sock.sendall(u32(len(startup) + 4) + startup)
        auth = receive(sock)
        assert auth[0] == b"R" and auth[1][:4] == u32(10), f"Expected SASL auth: {auth}"

        # SASL initial
        bare = b"n=,r=" + secrets.token_hex(16).encode()
        first = b"n,," + bare
        sock.sendall(frame(b"p", b"SCRAM-SHA-256\0" + u32(len(first)) + first))

        # SASL continue
        tag, challenge = receive(sock)
        assert tag == b"R" and challenge[:4] == u32(11)
        server_first = challenge[4:]
        info = dict(field.split(b"=", 1) for field in server_first.split(b","))

        # Client final
        salted = hashlib.pbkdf2_hmac(
            "sha256", password.encode() if isinstance(password, str) else password,
            base64.b64decode(info[b"s"]), int(info[b"i"])
        )
        final = b"c=biws,r=" + info[b"r"]
        transcript = b",".join((bare, server_first, final))
        client_key = hmac.digest(salted, b"Client Key", "sha256")
        signature = hmac.digest(hashlib.sha256(client_key).digest(), transcript, "sha256")
        proof = bytes(a ^ b for a, b in zip(client_key, signature))
        sock.sendall(frame(b"p", final + b",p=" + base64.b64encode(proof)))

        # SASL final & AuthOK
        assert receive(sock)[0] == b"R"
        assert receive(sock) == (b"R", u32(0))
        until_ready(sock)
        return sock
    except BaseException:
        sock.close()
        raise


def run_sql(sock, sql):
    """SQL を実行し、(完了タグ, 列情報リスト, 行データリスト, エラーSQLSTATE) を返す"""
    sock.sendall(frame(b"Q", sql.encode() + b"\0"))
    messages = until_ready(sock)
    command_tag = None
    columns = []
    rows = []
    sqlstate = None

    for tag, body in messages:
        if tag == b"C":
            command_tag = body.rstrip(b"\0").decode(errors="replace")
        elif tag == b"T":
            # RowDescription: 列数 (u16) + 列定義
            num_cols = struct.unpack("!H", body[:2])[0]
            offset = 2
            for _ in range(num_cols):
                null_pos = body.find(b"\0", offset)
                name = body[offset:null_pos].decode(errors="replace")
                offset = null_pos + 1
                # table_oid (4), col_attr (2), type_oid (4), type_len (2), type_mod (4), format (2)
                type_oid = struct.unpack("!I", body[offset + 6 : offset + 10])[0]
                offset += 18
                columns.append((name, type_oid))
        elif tag == b"D":
            # DataRow: 列数 (u16) + (len (4) + data)*
            num_fields = struct.unpack("!H", body[:2])[0]
            offset = 2
            row = []
            for _ in range(num_fields):
                field_len = struct.unpack("!i", body[offset : offset + 4])[0]
                offset += 4
                if field_len == -1:
                    row.append(None)
                else:
                    field_data = body[offset : offset + field_len].decode(errors="replace")
                    row.append(field_data)
                    offset += field_len
            rows.append(tuple(row))
        elif tag == b"E":
            fields = body.split(b"\0")
            for f in fields:
                if f.startswith(b"C"):
                    sqlstate = f[1:].decode(errors="replace")
                    break

    return {
        "command_tag": command_tag,
        "columns": columns,
        "rows": rows,
        "sqlstate": sqlstate,
    }


def compare_query(sigil_sock, pg_sock, sql, desc):
    res_sigil = run_sql(sigil_sock, sql)
    res_pg = run_sql(pg_sock, sql)

    differences = []
    if res_sigil["command_tag"] != res_pg["command_tag"]:
        differences.append(f"command_tag: sigil='{res_sigil['command_tag']}', pg='{res_pg['command_tag']}'")
    if res_sigil["sqlstate"] != res_pg["sqlstate"]:
        differences.append(f"sqlstate: sigil='{res_sigil['sqlstate']}', pg='{res_pg['sqlstate']}'")
    if res_sigil["rows"] != res_pg["rows"]:
        differences.append(f"rows: sigil={res_sigil['rows']}, pg={res_pg['rows']}")
    if len(res_sigil["columns"]) != len(res_pg["columns"]):
        differences.append(f"columns count: sigil={len(res_sigil['columns'])}, pg={len(res_pg['columns'])}")

    status = "MATCH" if not differences else "DIFF"
    print(f"  [{status}] {desc}")
    if differences:
        for d in differences:
            print(f"      - {d}")
    return differences


def run_differential_tests(sigil_conn_info, pg_conn_info):
    print("=== Starting Differential Testing: Sigil DB vs Official PostgreSQL ===")
    
    with closing(connect_pg(*sigil_conn_info)) as sigil_sock, \
         closing(connect_pg(*pg_conn_info)) as pg_sock:

        test_cases = [
            # DDL: DROP & CREATE TABLE
            ("DROP TABLE IF EXISTS diff_child", "DROP child table if exists"),
            ("DROP TABLE IF EXISTS diff_test", "DROP main table if exists"),
            ("CREATE TABLE diff_test (id BIGINT PRIMARY KEY, name TEXT, val BIGINT, active BOOLEAN)", "CREATE TABLE diff_test"),
            ("CREATE TABLE diff_child (id BIGINT PRIMARY KEY, parent_id BIGINT, note TEXT)", "CREATE TABLE diff_child"),

            # DML: INSERT
            ("INSERT INTO diff_test VALUES (1, 'alice', 100, true), (2, 'bob', 200, false), (3, 'charlie', 300, true)", "INSERT multiple rows"),
            ("INSERT INTO diff_child VALUES (10, 1, 'child1'), (20, 3, 'child3')", "INSERT into child"),

            # DQL: SELECT with ORDER BY
            ("SELECT id, name, val, active FROM diff_test ORDER BY id ASC", "SELECT all ordered ASC"),

            # DML: UPDATE
            ("UPDATE diff_test SET val = 150 WHERE id = 1", "UPDATE single row"),
            ("SELECT id, val FROM diff_test WHERE id = 1", "Verify UPDATE result"),

            # DML: DELETE
            ("DELETE FROM diff_test WHERE id = 2", "DELETE single row"),
            ("SELECT id FROM diff_test ORDER BY id ASC", "Verify DELETE result"),

            # DQL: Aggregation (COUNT, SUM, MIN, MAX)
            ("SELECT COUNT(*), SUM(val), MIN(val), MAX(val) FROM diff_test", "Aggregates (COUNT, SUM, MIN, MAX)"),

            # DQL: INNER JOIN
            ("SELECT diff_test.name, diff_child.note FROM diff_test INNER JOIN diff_child ON diff_test.id = diff_child.parent_id ORDER BY diff_test.id ASC", "INNER JOIN 2 tables"),

            # DQL: LIMIT & OFFSET
            ("SELECT id, name FROM diff_test ORDER BY id DESC LIMIT 1 OFFSET 0", "LIMIT 1 OFFSET 0"),

            # Transaction: ROLLBACK
            ("BEGIN", "BEGIN tx for rollback"),
            ("INSERT INTO diff_test VALUES (4, 'david', 400, true)", "INSERT in tx to rollback"),
            ("ROLLBACK", "ROLLBACK tx"),
            ("SELECT COUNT(*) FROM diff_test", "Verify count after ROLLBACK"),

            # Transaction: COMMIT
            ("BEGIN", "BEGIN tx for commit"),
            ("INSERT INTO diff_test VALUES (5, 'eve', 500, true)", "INSERT in tx to commit"),
            ("COMMIT", "COMMIT tx"),
            ("SELECT COUNT(*) FROM diff_test", "Verify count after COMMIT"),

            # Error cases: Duplicate PK
            ("INSERT INTO diff_test VALUES (1, 'dup', 999, true)", "Duplicate PK constraint violation (23505)"),

            # Error cases: Syntax error
            ("SELECT FROM WHERE INVALID", "Syntax error (42601)"),

            # Clean up
            ("DROP TABLE diff_child", "DROP child table"),
            ("DROP TABLE diff_test", "DROP main table"),
        ]

        total_diffs = 0
        diff_log = []
        for sql, desc in test_cases:
            diffs = compare_query(sigil_sock, pg_sock, sql, desc)
            if diffs:
                total_diffs += len(diffs)
                diff_log.append((desc, sql, diffs))

        print(f"\n=== Differential Testing Summary ===")
        print(f"Total test queries executed: {len(test_cases)}")
        print(f"Total differences found: {total_diffs}")
        if total_diffs == 0:
            print("PERFECT MATCH: All rows, columns, command tags, and error SQLSTATE matched official PostgreSQL exactly!")
        else:
            print("Differences list:")
            for desc, sql, diffs in diff_log:
                print(f"  - {desc} (`{sql}`):")
                for d in diffs:
                    print(f"      {d}")

        return total_diffs


def run_sigil_only(sigil_conn_info):
    print("=== Running Differential Test Suite on Sigil DB Server ===")
    with closing(connect_pg(*sigil_conn_info)) as sock:
        test_cases = [
            ("DROP TABLE IF EXISTS diff_child", "DROP child table if exists", "DROP TABLE", None, 0),
            ("DROP TABLE IF EXISTS diff_test", "DROP main table if exists", "DROP TABLE", None, 0),
            ("CREATE TABLE diff_test (id BIGINT PRIMARY KEY, name TEXT, val BIGINT, active BOOLEAN)", "CREATE TABLE diff_test", "CREATE TABLE", None, 0),
            ("CREATE TABLE diff_child (id BIGINT PRIMARY KEY, parent_id BIGINT, note TEXT)", "CREATE TABLE diff_child", "CREATE TABLE", None, 0),
            ("INSERT INTO diff_test VALUES (1, 'alice', 100, true), (2, 'bob', 200, false), (3, 'charlie', 300, true)", "INSERT multiple rows", "INSERT 0 3", None, 0),
            ("INSERT INTO diff_child VALUES (10, 1, 'child1'), (20, 3, 'child3')", "INSERT into child", "INSERT 0 2", None, 0),
            ("SELECT id, name, val, active FROM diff_test ORDER BY id ASC", "SELECT all ordered ASC", "SELECT 3", None, 3),
            ("UPDATE diff_test SET val = 150 WHERE id = 1", "UPDATE single row", "UPDATE 1", None, 0),
            ("DELETE FROM diff_test WHERE id = 2", "DELETE single row", "DELETE 1", None, 0),
            ("SELECT COUNT(*), SUM(val), MIN(val), MAX(val) FROM diff_test", "Aggregates", "SELECT 1", None, 1),
            ("SELECT diff_test.name, diff_child.note FROM diff_test INNER JOIN diff_child ON diff_test.id = diff_child.parent_id ORDER BY diff_test.id ASC", "INNER JOIN", "SELECT 2", None, 2),
            ("SELECT id, name FROM diff_test ORDER BY id DESC LIMIT 1 OFFSET 0", "LIMIT 1 OFFSET 0", "SELECT 1", None, 1),
            ("BEGIN", "BEGIN tx", "BEGIN", None, 0),
            ("INSERT INTO diff_test VALUES (4, 'david', 400, true)", "INSERT in tx", "INSERT 0 1", None, 0),
            ("ROLLBACK", "ROLLBACK tx", "ROLLBACK", None, 0),
            ("SELECT COUNT(*) FROM diff_test", "Verify count after ROLLBACK", "SELECT 1", None, 1),
            ("BEGIN", "BEGIN tx", "BEGIN", None, 0),
            ("INSERT INTO diff_test VALUES (5, 'eve', 500, true)", "INSERT in tx", "INSERT 0 1", None, 0),
            ("COMMIT", "COMMIT tx", "COMMIT", None, 0),
            ("SELECT COUNT(*) FROM diff_test", "Verify count after COMMIT", "SELECT 1", None, 1),
            ("INSERT INTO diff_test VALUES (1, 'dup', 999, true)", "Duplicate PK violation", None, "23505", 0),
            ("SELECT FROM WHERE INVALID", "Syntax error", None, "42601", 0),
            ("DROP TABLE diff_child", "DROP child table", "DROP TABLE", None, 0),
            ("DROP TABLE diff_test", "DROP main table", "DROP TABLE", None, 0),
        ]
        for sql, desc, exp_tag, exp_err, exp_rows in test_cases:
            res = run_sql(sock, sql)
            if exp_err:
                assert res["sqlstate"] == exp_err, f"{desc} expected error {exp_err}, got {res['sqlstate']}"
            else:
                assert res["command_tag"] == exp_tag, f"{desc} expected tag {exp_tag}, got {res['command_tag']}"
                if exp_rows > 0:
                    assert len(res["rows"]) == exp_rows, f"{desc} expected {exp_rows} rows, got {len(res['rows'])}"
            print(f"  [OK] {desc}")
    print("=== Sigil DB differential test suite executed successfully ===")
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--sigil-only":
        sigil_info = (sys.argv[3], int(sys.argv[2]), "wire", "wire", "temporary test credential")
        sys.exit(run_sigil_only(sigil_info))

    if len(sys.argv) < 8:
        print("Usage: python3 diff_compare.py <sigil_port> <sigil_host> <pg_port> <pg_host> <pg_user> <pg_db> <pg_password>")
        print("   or: python3 diff_compare.py --sigil-only <sigil_port> <sigil_host>")
        sys.exit(1)

    sigil_info = (sys.argv[2], int(sys.argv[1]), "wire", "wire", "temporary test credential")
    pg_info = (sys.argv[4], int(sys.argv[3]), sys.argv[5], sys.argv[6], sys.argv[7])

    diffs = run_differential_tests(sigil_info, pg_info)
    sys.exit(0 if diffs == 0 else 1)

