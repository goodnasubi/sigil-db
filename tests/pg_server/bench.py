"""ADR 0046 完了条件 6: 性能と上限の記録

スループット（QPS）、同時接続スケーラビリティ、クエリ遅延を測定・記録する。
上限（同時接続 64、SQL 長 64 KiB、パラメータ 256 個、パラメータ長 4096 byte 等）
の到達・超過時の挙動を測定し、結果を記録する。
"""

import socket
import sys
import threading
import time
from contextlib import closing

from loopback import (
    PASSWORD,
    bind,
    command,
    connect,
    exact,
    execute,
    extended,
    frame,
    parse,
    query,
    receive,
    sqlstate,
    u16,
    u32,
    until_ready,
)


def measure_latency_and_qps(port, host, iterations=200):
    print("  - Measuring query latency and QPS...")
    latencies = []
    with closing(connect(port, host)) as sock:
        # ウォームアップ
        for _ in range(10):
            query(sock, "SELECT id FROM wire_rows")

        start = time.monotonic()
        for _ in range(iterations):
            t0 = time.monotonic()
            query(sock, "SELECT id FROM wire_rows")
            t1 = time.monotonic()
            latencies.append((t1 - t0) * 1000.0)  # ms
        total_time = time.monotonic() - start

    qps = iterations / total_time
    latencies.sort()
    min_lat = latencies[0]
    p50_lat = latencies[len(latencies) // 2]
    p95_lat = latencies[int(len(latencies) * 0.95)]
    max_lat = latencies[-1]
    avg_lat = sum(latencies) / len(latencies)

    print(f"    [Single Connection QPS]: {qps:.1f} queries/sec (total {iterations} in {total_time:.3f}s)")
    print(f"    [Latency (ms)]: min={min_lat:.3f}, p50={p50_lat:.3f}, p95={p95_lat:.3f}, max={max_lat:.3f}, avg={avg_lat:.3f}")
    return qps, avg_lat


def measure_concurrency_scalability(port, host, concurrency_levels=(1, 4, 16, 32)):
    print("  - Measuring concurrent connection scalability...")
    for c in concurrency_levels:
        queries_per_thread = 20
        total_queries = c * queries_per_thread
        errors = []

        def worker():
            try:
                with closing(connect(port, host)) as sock:
                    for _ in range(queries_per_thread):
                        query(sock, "SELECT id FROM wire_rows")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(c)]
        t0 = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.monotonic() - t0
        qps = total_queries / elapsed
        assert not errors, f"Errors during concurrency {c}: {errors}"
        print(f"    [Concurrency {c:2d}]: {qps:.1f} QPS ({total_queries} queries in {elapsed:.3f}s)")


def test_resource_limits(port, host):
    print("  - Measuring resource bounds and limit enforcement...")
    
    # 1. 同時接続数上限 (64)
    print("    * Connection limit (64 concurrent)...")
    conns = []
    try:
        for _ in range(64):
            conns.append(socket.create_connection((host, port), timeout=5))
        # 65本目: 即時拒否 (SQLSTATE 53300)
        with closing(socket.create_connection((host, port), timeout=5)) as rejected:
            rep = receive(rejected)
            sqlstate(rep, b"53300")
            print("      -> Connection #65 correctly rejected with SQLSTATE 53300")
    finally:
        for c in conns:
            c.close()

    # 接続解放待ち
    time.sleep(0.1)

    with closing(connect(port, host)) as sock:
        # 2. SQL 文長上限 (64 KiB = 65536 byte)
        print("    * SQL statement length limit (64 KiB)...")
        # 64 KiB 未満: 成功
        valid_spaces = " " * 1000
        query(sock, f"SELECT id FROM wire_rows WHERE id = 1{valid_spaces}")
        # 64 KiB 超過 (65537 byte): 構文エラー 42601
        huge_sql = "SELECT id FROM wire_rows WHERE id = 1" + " " * 65537
        rep = query(sock, huge_sql)[0]
        sqlstate(rep, b"42601")
        print("      -> SQL length > 64 KiB correctly rejected with SQLSTATE 42601")

        # 3. パラメータ値長上限 (4096 byte)
        print("    * Query parameter length limit (4096 byte)...")
        # 4096 byte: 成功
        extended(
            sock,
            parse(b"p_len", b"SELECT $1 FROM wire_rows WHERE id = 1", [25]),
            bind(b"b_len", b"p_len", [b"a" * 4096]),
            execute(b"b_len"),
        )
        # 4097 byte: 54000 エラー
        rep = extended(sock, bind(b"b_len2", b"p_len", [b"a" * 4097]), execute(b"b_len2"))
        sqlstate(rep[0], b"54000")
        print("      -> Parameter length > 4096 byte correctly rejected with SQLSTATE 54000")

        # 4. パラメータ数上限 (256 個)
        print("    * Parameter count limit (256 parameters)...")
        # 256 個: 成功
        placeholders_256 = ", ".join(f"${i+1}" for i in range(256))
        oids_256 = [20] * 256
        values_256 = [b"1"] * 256
        extended(
            sock,
            parse(b"p_count", f"SELECT {placeholders_256} FROM wire_rows WHERE id = 1".encode(), oids_256),
            bind(b"b_count", b"p_count", values_256),
            execute(b"b_count"),
        )
        # 257 個: 54000 拒否
        placeholders_257 = ", ".join(f"${i+1}" for i in range(257))
        oids_257 = [20] * 257
        rep = extended(sock, parse(b"p_over", f"SELECT {placeholders_257} FROM wire_rows WHERE id = 1".encode(), oids_257))
        sqlstate(rep[-1], b"54000")
        print("      -> Parameter count > 256 correctly rejected with SQLSTATE 54000")

        # 5. ステートメント数上限 (1 接続あたり 64 個)
        print("    * Statement count limit (64 per session)...")
        for i in range(63):
            extended(sock, parse(f"s_{i}".encode(), b"SELECT id FROM wire_rows"))
        # 65 個目の Parse: 54000 拒否
        rep = extended(sock, parse(b"s_overflow", b"SELECT id FROM wire_rows"))
        sqlstate(rep[-1], b"54000")
        print("      -> Statement count > 64 correctly rejected with SQLSTATE 54000")


def main(port, host="127.0.0.1"):
    print(f"=== Condition 6: Performance benchmark and resource limits verification ({host}:{port}) ===")
    start_time = time.monotonic()
    measure_latency_and_qps(port, host)
    measure_concurrency_scalability(port, host)
    test_resource_limits(port, host)
    elapsed = time.monotonic() - start_time
    print(f"=== Condition 6 passed: Performance & limits verified in {elapsed:.2f}s ===")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 bench.py <port> [host]")
        sys.exit(1)
    port = int(sys.argv[1])
    host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    main(port, host)
