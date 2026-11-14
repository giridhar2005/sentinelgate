"""Lightweight benchmark for SentinelGate.
Measures pure proxy overhead, semantic cache hit latency, and RAM usage.
"""

import asyncio
import os
import platform
import resource
import sys
import time
from typing import List

# Add repository root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx
import numpy as np
import respx
from starlette.testclient import TestClient

from app.main import create_app


def get_process_rss_mb() -> float:
    """Returns Resident Set Size in megabytes."""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if platform.system() == "Darwin":
        return round(raw / (1024 * 1024), 2)
    return round(raw / 1024, 2)


async def main():
    print("==================================================")
    print("        SentinelGate Performance Benchmark        ")
    print("==================================================")

    bench_db = "bench_temp.db"
    for f in [bench_db, f"{bench_db}-wal", f"{bench_db}-shm"]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass

    idle_rss = get_process_rss_mb()
    print(f"[*] Process Idle RAM (RSS): {idle_rss:.2f} MB")

    app = create_app(db_path=bench_db)
    app.state.evals.sample_rate = 0.0

    loaded_rss = get_process_rss_mb()
    print(f"[*] Process RAM with Model (RSS): {loaded_rss:.2f} MB")

    test_client = TestClient(app)
    app.state.router.client = None

    PROMPTS = [
        "What is connection pooling?",
        "Explain connection pooling in HTTP clients.",
        "How do database indexes improve query performance?",
        "Explain B-tree indexes in relational databases.",
        "What is the difference between TCP and UDP?",
        "Compare TCP and UDP transport protocols.",
        "How does Redis implement in-memory key-value storage?",
        "Explain Redis memory architecture and persistence.",
        "What is WAL mode in SQLite?",
        "Why is SQLite WAL mode faster for concurrent readers?",
    ]

    # 100 requests pool (10 unique semantic pairs repeated)
    pool = [PROMPTS[i % len(PROMPTS)] for i in range(100)]

    with respx.mock(assert_all_called=False) as mock:
        app.state.router.client = None

        mock.post(url__startswith="https://api.openai.com").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "chatcmpl-mock-bench",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "gpt-4o-mini",
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": "Simulated upstream response."},
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 16, "total_tokens": 28},
                },
            )
        )

        all_latencies: List[float] = []
        cached_latencies: List[float] = []
        uncached_latencies: List[float] = []
        cache_hits = 0

        t_start = time.perf_counter()

        for prompt in pool:
            t0 = time.perf_counter()
            res = test_client.post(
                "/v1/chat/completions",
                json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}]},
            )
            elapsed = (time.perf_counter() - t0) * 1000.0
            all_latencies.append(elapsed)

            if res.status_code == 200:
                if res.headers.get("X-Cache") == "HIT":
                    cache_hits += 1
                    cached_latencies.append(elapsed)
                else:
                    uncached_latencies.append(elapsed)

        wall_time = time.perf_counter() - t_start
        active_rss = get_process_rss_mb()

    # Metrics
    all_latencies.sort()
    p50 = float(np.percentile(all_latencies, 50))
    p95 = float(np.percentile(all_latencies, 95))
    p99 = float(np.percentile(all_latencies, 99))
    avg_cached = float(np.mean(cached_latencies)) if cached_latencies else 0.0
    avg_uncached = float(np.mean(uncached_latencies)) if uncached_latencies else 0.0
    min_latency = min(all_latencies)

    print("\n---------------- Results ----------------")
    print(f"Total Requests              : {len(pool)}")
    print(f"Wall Clock Time             : {wall_time:.2f} s")
    print(f"Cache Hit Rate              : {cache_hits}/{len(pool)} ({cache_hits/len(pool)*100:.1f}%)")
    print(f"Min Gateway Overhead        : {min_latency:.2f} ms")
    print(f"Avg Semantic Cache Latency  : {avg_cached:.2f} ms")
    print(f"Avg Uncached Proxy Latency  : {avg_uncached:.2f} ms")
    print(f"P50 Latency                 : {p50:.2f} ms")
    print(f"P95 Latency                 : {p95:.2f} ms")
    print(f"P99 Latency                 : {p99:.2f} ms")
    print(f"Active RAM Usage (RSS)      : {active_rss:.2f} MB")
    print("-----------------------------------------")

    print("\nMarkdown Table for README:\n")
    print("| Metric | Measured Result |")
    print("|---|---|")
    print(f"| **Pure Gateway Overhead (Min)** | **{min_latency:.2f} ms** |")
    print(f"| **Semantic Cache Hit Latency (Avg)** | **{avg_cached:.2f} ms** |")
    print(f"| **Cache Miss Latency (Upstream + Proxy)** | {avg_uncached:.2f} ms |")
    print(f"| **Cache Hit Rate** | **{cache_hits/len(pool)*100:.1f}%** ({cache_hits}/{len(pool)}) |")
    print(f"| **Latency P50 / P95** | {p50:.2f} ms / {p95:.2f} ms |")
    print(f"| **Idle RAM (Base)** | {idle_rss:.1f} MB |")
    print(f"| **Active RAM (Loaded ONNX Runtime)** | **{active_rss:.1f} MB** |")

    # Cleanup
    for f in [bench_db, f"{bench_db}-wal", f"{bench_db}-shm"]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
