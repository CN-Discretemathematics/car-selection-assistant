"""轻量并发压测脚本（阶段 8：并发压测）。

用法（先启动后端）：
    python tools/load_test.py --url http://127.0.0.1:8000 --concurrency 20 --requests 500

输出：总请求数、成功率、吞吐（req/s）、P50/P90/P99 延迟。
只依赖标准库（本环境 http 客户端受沙箱限制，httpx 走回环会被 502，故用 http.client）；
用于 1000 用户量级的初步容量验证，正式压测用专业工具。
"""
from __future__ import annotations

import argparse
import http.client
import statistics
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

PATHS = ["/api/v1/health", "/api/v1/home", "/api/v1/brands"]


def _request(host: str, port: int, path: str) -> tuple[float, str | None]:
    start = time.perf_counter()
    try:
        conn = http.client.HTTPConnection(host, port, timeout=10.0)
        conn.request("GET", path)
        resp = conn.getresponse()
        resp.read()
        status = resp.status
        conn.close()
        if status != 200:
            return time.perf_counter() - start, f"{path}: HTTP {status}"
        return time.perf_counter() - start, None
    except Exception as err:  # noqa: BLE001
        return time.perf_counter() - start, f"{path}: {type(err).__name__}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--requests", type=int, default=500)
    args = parser.parse_args(argv)
    if args.concurrency < 1 or args.requests < 1:
        print("--concurrency/--requests 必须为正整数", file=sys.stderr)
        return 1

    parsed = urllib.parse.urlsplit(args.url)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 80

    latencies: list[float] = []
    errors: list[str] = []
    lock = threading.Lock()

    def task(index: int) -> None:
        latency, error = _request(host, port, PATHS[index % len(PATHS)])
        with lock:
            latencies.append(latency)
            if error:
                errors.append(error)

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        list(pool.map(task, range(args.requests)))
    elapsed = time.perf_counter() - started

    latencies.sort()
    pct = lambda p: latencies[min(len(latencies) - 1, int(len(latencies) * p))] * 1000  # noqa: E731
    print(f"总请求：{len(latencies)}，成功：{len(latencies) - len(errors)}，失败：{len(errors)}")
    print(f"并发：{args.concurrency}，耗时：{elapsed:.2f}s，吞吐：{len(latencies) / elapsed:.1f} req/s")
    if latencies:
        print(
            f"延迟：均值 {statistics.mean(latencies) * 1000:.1f}ms，"
            f"P50 {pct(0.5):.1f}ms，P90 {pct(0.9):.1f}ms，P99 {pct(0.99):.1f}ms"
        )
    if errors:
        from collections import Counter

        for message, count in Counter(errors).most_common(5):
            print(f"  错误 {count}x：{message}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
