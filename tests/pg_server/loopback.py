"""Exercise the actual TCP server with std.pg and selected wire messages."""

import base64
import hashlib
import hmac
import os
import secrets
import selectors
import socket
import struct
import subprocess
import sys
import tempfile
import time
from contextlib import closing

PASSWORD = b"temporary test credential"


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
            raise EOFError("unexpected PostgreSQL disconnect")
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


class AtCapacity(Exception):
    pass


def connect(port, host):
    sock = socket.create_connection((host, port), timeout=5)
    try:
        startup = u32(196608) + b"user\0wire\0database\0wire\0\0"
        sock.sendall(u32(len(startup) + 4) + startup)
        authentication = receive(sock)
        if authentication[0] == b"E" and b"C53300" in authentication[1].split(b"\0"):
            raise AtCapacity()
        assert authentication == (b"R", u32(10) + b"SCRAM-SHA-256\0\0"), authentication
        bare = b"n=,r=" + secrets.token_hex(16).encode()
        first = b"n,," + bare
        sock.sendall(frame(b"p", b"SCRAM-SHA-256\0" + u32(len(first)) + first))
        tag, challenge = receive(sock)
        assert tag == b"R" and challenge[:4] == u32(11)
        server_first = challenge[4:]
        info = dict(field.split(b"=", 1) for field in server_first.split(b","))
        salted = hashlib.pbkdf2_hmac(
            "sha256", PASSWORD, base64.b64decode(info[b"s"]), int(info[b"i"])
        )
        final = b"c=biws,r=" + info[b"r"]
        transcript = b",".join((bare, server_first, final))
        client_key = hmac.digest(salted, b"Client Key", "sha256")
        signature = hmac.digest(hashlib.sha256(client_key).digest(), transcript, "sha256")
        proof = bytes(a ^ b for a, b in zip(client_key, signature))
        sock.sendall(frame(b"p", final + b",p=" + base64.b64encode(proof)))
        server_key = hmac.digest(salted, b"Server Key", "sha256")
        server_proof = hmac.digest(server_key, transcript, "sha256")
        assert receive(sock) == (b"R", u32(12) + b"v=" + base64.b64encode(server_proof))
        assert receive(sock) == (b"R", u32(0))
        assert until_ready(sock)[-1] == (b"Z", b"I")
        return sock
    except BaseException:
        sock.close()
        raise


def query(sock, sql, status=b"I"):
    sock.sendall(frame(b"Q", sql.encode() + b"\0"))
    messages = until_ready(sock)
    assert messages[-1] == (b"Z", status), messages
    return messages[:-1]


def sqlstate(reply, expected):
    tag, payload = reply
    assert tag == b"E" and b"C" + expected in payload.split(b"\0"), reply


def command(sock, sql, expected, status=b"I"):
    messages = query(sock, sql, status)
    assert messages[-1] == (b"C", expected + b"\0"), messages
    return messages


def parse(name, sql, oids=()):
    return frame(b"P", name + b"\0" + sql + b"\0" + u16(len(oids))
                 + b"".join(u32(oid) for oid in oids))


def bind(portal, statement, values=()):
    return frame(b"B", portal + b"\0" + statement + b"\0" + u16(0)
                 + u16(len(values))
                 + b"".join(u32(len(value)) + value for value in values)
                 + u16(0))


def execute(name, count=0):
    return frame(b"E", name + b"\0" + u32(count))


def extended(sock, *requests, status=b"I"):
    sock.sendall(b"".join(requests) + frame(b"S"))
    replies = until_ready(sock)
    assert replies[-1] == (b"Z", status), replies
    return replies[:-1]


def tags(replies):
    return b"".join(tag for tag, _ in replies)


def test_queries(port, host):
    with closing(connect(port, host)) as sock:
        command(sock, "CREATE TABLE wire_rows (id BIGINT PRIMARY KEY, note TEXT)", b"CREATE TABLE")
        command(sock, "INSERT INTO wire_rows VALUES (1, 'one'), (2, 'two')", b"INSERT 0 2")
        assert tags(query(sock, "SELECT note FROM wire_rows WHERE id = 1")) == b"TDC"
        assert query(sock, " -- empty") == [(b"I", b"")]
        sqlstate(query(sock, "DELETE FROM wire_rows; DELETE FROM wire_rows")[0], b"42601")
        sqlstate(query(sock, " " * 65537)[0], b"42601")

        command(sock, "BEGIN", b"BEGIN", b"T")
        replies = extended(
            sock,
            parse(b"find", b"SELECT note FROM wire_rows WHERE id >= $1 ORDER BY id", [20]),
            frame(b"D", b"Sfind\0"),
            bind(b"rows", b"find", [b"1"]),
            frame(b"D", b"Prows\0"),
            execute(b"rows", 1),
            status=b"T",
        )
        assert tags(replies) == b"1tT2TDs", replies
        assert replies[1] == (b"t", u16(1) + u32(20))
        assert replies[2][1] == replies[4][1]
        assert replies[5] == (b"D", u16(1) + u32(3) + b"one")
        replies = extended(sock, execute(b"rows", 1), frame(b"C", b"Prows\0"), status=b"T")
        assert replies == [(b"D", u16(1) + u32(3) + b"two"), (b"C", b"SELECT 2\0"), (b"3", b"")]
        command(sock, "COMMIT", b"COMMIT")

        replies = extended(sock, bind(b"", b"find", [b"x" * 4097]), execute(b""))
        assert len(replies) == 1
        sqlstate(replies[0], b"54000")
        assert tags(extended(sock, parse(b"", b""), bind(b"", b""), execute(b""), execute(b""))) == b"12II"
        assert tags(extended(sock, frame(b"C", b"Sfind\0"))) == b"3"
        sqlstate(extended(sock, bind(b"", b"find", [b"1"]))[0], b"26000")

        replies = extended(sock, *(parse(str(i).encode(), b"BEGIN") for i in range(65)))
        assert tags(replies) == b"1" * 63 + b"E"
        sqlstate(replies[-1], b"54000")
        command(sock, "DISCARD ALL", b"DISCARD ALL")
        assert tags(extended(sock, parse(b"after_discard", b"BEGIN"))) == b"1"
        replies = extended(sock, *(bind(str(i).encode(), b"after_discard") for i in range(65)))
        assert tags(replies) == b"2" * 64 + b"E"
        sqlstate(replies[-1], b"54000")

        for tag in (b"H", b"F", b"d", b"c", b"f"):
            sqlstate(extended(sock, frame(tag))[0], b"0A000")
        command(sock, "BEGIN", b"BEGIN", b"T")
        assert tags(command(sock, "BEGIN", b"BEGIN", b"T")) == b"NC"
        sqlstate(query(sock, "invalid SQL", b"E")[0], b"42601")
        sqlstate(query(sock, "DELETE FROM wire_rows", b"E")[0], b"25P02")
        command(sock, "COMMIT", b"ROLLBACK")

        replies = extended(
            sock, parse(b"minimum", b"SELECT $1 FROM wire_rows WHERE id = 1", [20]),
            bind(b"", b"minimum", [b"-9223372036854775808"]), execute(b""),
        )
        assert replies[-2] == (b"D", u16(1) + u32(20) + b"-9223372036854775808"), replies


def test_serialization(port, host):
    with closing(connect(port, host)) as first, closing(connect(port, host)) as second:
        command(first, "BEGIN", b"BEGIN", b"T")
        command(first, "INSERT INTO wire_rows VALUES (3, 'committed')", b"INSERT 0 1", b"T")
        sqlstate(query(second, "SELECT id FROM wire_rows WHERE id = 3")[0], b"57014")
        second.sendall(frame(b"Q", b"SELECT id FROM wire_rows WHERE id = 3\0"))
        command(first, "COMMIT", b"COMMIT")
        replies = until_ready(second)
        assert tags(replies) == b"TDCZ" and replies[-2] == (b"C", b"SELECT 1\0"), replies

        command(first, "BEGIN", b"BEGIN", b"T")
        command(first, "DELETE FROM wire_rows WHERE id = 3", b"DELETE 1", b"T")
        second.sendall(frame(b"Q", b"SELECT id FROM wire_rows WHERE id = 3\0"))
        first.close()  # Unexpected EOF while owning the transaction.
        replies = until_ready(second)
        assert tags(replies) == b"TDCZ" and replies[-2] == (b"C", b"SELECT 1\0"), replies


def test_bad_message(port, host):
    for request in (frame(b"?", b""), frame(b"Q", b"\xff\0")):
        with closing(connect(port, host)) as sock:
            sock.sendall(request)
            sqlstate(receive(sock), b"08P01")
            assert sock.recv(1) == b""


def test_capacity(port, host):
    clients = []
    try:
        for _ in range(64):
            clients.append(socket.create_connection((host, port), timeout=5))
        with closing(socket.create_connection((host, port), timeout=5)) as rejected:
            sqlstate(receive(rejected), b"53300")
        clients.pop().close()
    finally:
        for client in clients:
            client.close()

    deadline = time.monotonic() + 5
    while True:
        try:
            sock = connect(port, host)
            break
        except AtCapacity:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)
    with closing(sock):
        command(sock, "BEGIN", b"BEGIN", b"T")
        command(sock, "DELETE FROM wire_rows", b"DELETE 3", b"T")
        sock.sendall(b"Q" + u32(16777217))
        sqlstate(receive(sock), b"54000")
        assert sock.recv(1) == b""
    with closing(connect(port, host)) as sock:
        command(sock, "SELECT id FROM wire_rows", b"SELECT 3")


def main(server, pool_client, workers, host):
    with tempfile.TemporaryDirectory(prefix="sigil-pg-server-") as path:
        options = {
            **os.environ,
            "SIGIL_WORKERS": workers,
            "SIGIL_DB_DIR": os.path.join(path, "db"),
            "SIGIL_DB_USER": "wire",
            "SIGIL_DB_NAME": "wire",
            "SIGIL_DB_PASSWORD": PASSWORD.decode(),
            "SIGIL_DB_PORT": "0",
            "SIGIL_DB_HOST": host,
            "SIGIL_DB_WAIT_MS": "500",
        }
        with subprocess.Popen([server], env=options, stdout=subprocess.PIPE, text=True) as proc:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(proc.stdout, selectors.EVENT_READ)
                    assert selector.select(timeout=15), "server did not announce a listening port"
                port = int(proc.stdout.readline())
                completed = subprocess.run(
                    [pool_client, host, str(port)],
                    env=options, capture_output=True, text=True, check=True, timeout=30,
                )
                assert completed.stdout.splitlines()[-1:] == ["0"], completed.stdout
                test_queries(port, host)
                test_serialization(port, host)
                test_capacity(port, host)
                test_bad_message(port, host)
                assert proc.poll() is None, "server stopped during the loopback tests"
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
    print(f"PostgreSQL v3 server loopback passed ({host}, workers={workers})")


if __name__ == "__main__":
    main(*sys.argv[1:])
