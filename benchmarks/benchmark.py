from __future__ import annotations

import asyncio
import os
import platform
import resource
import sys
import time
from typing import List

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx
import respx
from starlette.testclient import TestClient

from app.main import create_app


def get_memory_rss_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # On macOS ru_maxrss is in bytes; on Linux in kilobytes
    if platform.system() == "Darwin":
        return round(usage / (1024 * 1024), 2)
    return round(usage / 1024, 2)


async def run_benchmark():
    print("\n" + "=" * 65)
    print("      🚀 SENTINELGATE ENTERPRISE CONCURRENT BENCHMARK")
    print("=" * 65)

    bench_db = "sentinelgate_bench.db"
    for f in [bench_db, f"{bench_db}-wal", f"{bench_db}-shm"]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass

    app = create_app(db_path=bench_db)
    # Disable background evals for clean benchmarking
    app.state.evals.sample_rate = 0.0

    initial_rss = get_memory_rss_mb()
    print(f"[*] Baseline Process Memory (RSS) : {initial_rss} MB")

    test_client = TestClient(app)
    app.state.router.client = None

    PROMPTS = [
        "What is quantum computing?",
        "Explain quantum computing in simple terms.",
        "How does quantum superposition work?",
        "Tell me about qubits in quantum computing.",
        "What is machine learning?",
        "Explain machine learning fundamentals.",
        "How do neural networks work?",
        "Explain backpropagation in deep learning.",
        "What is Docker containerization?",
        "How do Kubernetes pods communicate?",
    ]

    # Generate 100 requests (repeating semantic clusters)
    requests_pool = [PROMPTS[i % len(PROMPTS)] for i in range(100)]

    with respx.mock(assert_all_called=False) as mock:
        app.state.router.client = None

        mock.post(url__startswith="https://api.openai.com").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "chatcmpl-mock",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "gpt-4o-mini",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "Simulated upstream response.",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 15, "completion_tokens": 20, "total_tokens": 35},
                },
            )
        )

        mock.post(url__startswith="https://api.groq.com").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "chatcmpl-groq-mock",
                    "object": "chat.completion",
                    "model": "llama-3.1-8b-instant",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Groq simulated response."},
                        }
                    ],
                    "usage": {"prompt_tokens": 15, "completion_tokens": 20, "total_tokens": 35},
                },
            )
        )

        latencies: List[float] = []
        cached_latencies: List[float] = []
        uncached_latencies: List[float] = []
        cache_hits = 0

        start_all = time.perf_counter()

        for prompt in requests_pool:
            t0 = time.perf_counter()
            resp = test_client.post(
                "/v1/chat/completions",
                json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}]},
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            latencies.append(elapsed_ms)

            if resp.status_code == 200:
                is_hit = resp.headers.get("X-Cache") == "HIT"
                if is_hit:
                    cache_hits += 1
                    cached_latencies.append(elapsed_ms)
                else:
                    uncached_latencies.append(elapsed_ms)

        total_time = time.perf_counter() - start_all
        final_rss = get_memory_rss_mb()

    # Percentiles
    import numpy as np

    latencies.sort()
    p50 = float(np.percentile(latencies, 50))
    p90 = float(np.percentile(latencies, 90))
    p95 = float(np.percentile(latencies, 95))
    p99 = float(np.percentile(latencies, 99))

    avg_cached = float(np.mean(cached_latencies)) if cached_latencies else 0.0
    avg_uncached = float(np.mean(uncached_latencies)) if uncached_latencies else 0.0
    speedup = (avg_uncached / avg_cached) if avg_cached > 0 else 1.0
    pure_overhead = min(latencies)

    print("\n" + "=" * 65)
    print("                    BENCHMARK RESULTS")
    print("=" * 65)
    print(f"Total Requests Processed : {len(requests_pool)}")
    print(f"Wall Clock Time          : {total_time:.2f} s")
    print(
        f"Cache Hit Rate           : {cache_hits}/{len(requests_pool)} ({cache_hits / len(requests_pool) * 100:.1f}%)"
    )
    print(f"Avg Semantic Cache Latency: {avg_cached:.2f} ms")
    print(f"Avg Uncached Latency     : {avg_uncached:.2f} ms")
    print(f"Semantic Cache Speedup   : {speedup:.1f}x faster")
    print(f"Pure Gateway Overhead    : {pure_overhead:.2f} ms")
    print(f"Memory Footprint (RSS)   : {final_rss:.2f} MB")
    print(f"P50 Latency              : {p50:.2f} ms")
    print(f"P90 Latency              : {p90:.2f} ms")
    print(f"P95 Latency              : {p95:.2f} ms")
    print(f"P99 Latency              : {p99:.2f} ms")
    print("=" * 65)

    print("\n### Copy-Paste Markdown Table for README.md:\n")
    print("| Metric | SentinelGate Result |")
    print("|---|---|")
    print(f"| **Total Requests** | {len(requests_pool)} |")
    print(
        f"| **Cache Hit Rate** | **{cache_hits / len(requests_pool) * 100:.1f}%** ({cache_hits}/{len(requests_pool)}) |"
    )
    print(f"| **Average Semantic Cache Latency** | **{avg_cached:.2f} ms** |")
    print(f"| **Average Uncached Latency** | {avg_uncached:.2f} ms |")
    print(f"| **Semantic Cache Speedup** | **{speedup:.1f}x faster** |")
    print(f"| **Pure Gateway Overhead** | **{pure_overhead:.2f} ms** |")
    print(f"| **Memory Footprint (RSS)** | **{final_rss:.1f} MB** |")
    print(f"| **Latency P50** | {p50:.2f} ms |")
    print(f"| **Latency P95** | {p95:.2f} ms |")
    print(f"| **Latency P99** | {p99:.2f} ms |")

    # Clean up benchmark temporary db files
    for f in ["sentinelgate_bench.db", "sentinelgate_bench.db-wal", "sentinelgate_bench.db-shm"]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass


if __name__ == "__main__":
    asyncio.run(run_benchmark())
