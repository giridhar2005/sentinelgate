"""Lightweight, reproducible benchmark for SentinelGate.

Measures in-process gateway overhead, semantic cache hit latency,
and RAM usage, exporting raw metrics to JSON for CI/README generation.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import resource
import sys
import time
from typing import Any, Dict, List

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


def run_benchmark(output_path: str = "benchmarks/results.json", num_requests: int = 100) -> Dict[str, Any]:
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

    pool = [PROMPTS[i % len(PROMPTS)] for i in range(num_requests)]

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

    # Metrics computation
    all_latencies.sort()
    p50 = float(np.percentile(all_latencies, 50))
    p95 = float(np.percentile(all_latencies, 95))
    p99 = float(np.percentile(all_latencies, 99))
    avg_cached = float(np.mean(cached_latencies)) if cached_latencies else 0.0
    avg_uncached = float(np.mean(uncached_latencies)) if uncached_latencies else 0.0
    min_latency = min(all_latencies) if all_latencies else 0.0
    throughput = round(len(pool) / max(0.0001, wall_time), 1)

    is_ci = os.getenv("GITHUB_ACTIONS") == "true"
    runner_desc = (
        f"GitHub Actions Runner ({os.getenv('RUNNER_OS', 'Linux')} {os.getenv('RUNNER_ARCH', 'X64')})"
        if is_ci
        else f"Local System ({platform.system()} {platform.machine()})"
    )

    results: Dict[str, Any] = {
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "runner": {
            "os": platform.system(),
            "release": platform.release(),
            "arch": platform.machine(),
            "python": platform.python_version(),
            "is_ci": is_ci,
            "description": runner_desc,
        },
        "config": {
            "total_requests": len(pool),
            "concurrency": 1,
            "protocol": "In-process TestClient (no network hops)",
            "model": "gpt-4o-mini",
            "embedding_model": "BAAI/bge-small-en-v1.5 (CPU ONNX)",
        },
        "metrics": {
            "cache_hits": cache_hits,
            "total_requests": len(pool),
            "cache_hit_rate_pct": round(cache_hits / len(pool) * 100.0, 1),
            "min_overhead_ms": round(min_latency, 2),
            "avg_cached_latency_ms": round(avg_cached, 2),
            "avg_uncached_latency_ms": round(avg_uncached, 2),
            "p50_ms": round(p50, 2),
            "p95_ms": round(p95, 2),
            "p99_ms": round(p99, 2),
            "wall_time_sec": round(wall_time, 3),
            "in_process_throughput_req_per_sec": throughput,
            "idle_ram_mb": round(idle_rss, 1),
            "active_ram_mb": round(active_rss, 1),
        },
    }

    print("\n---------------- Results ----------------")
    print(f"Runner                      : {runner_desc}")
    print(f"Python Version              : {platform.python_version()}")
    print(f"Total Requests              : {len(pool)}")
    print(f"Wall Clock Time             : {wall_time:.2f} s")
    print(f"Cache Hit Rate              : {cache_hits}/{len(pool)} ({results['metrics']['cache_hit_rate_pct']}%)")
    print(f"Min In-Process Overhead     : {min_latency:.2f} ms")
    print(f"Avg Semantic Cache Latency  : {avg_cached:.2f} ms")
    print(f"P50 Latency                 : {p50:.2f} ms")
    print(f"P95 Latency                 : {p95:.2f} ms")
    print(f"P99 Latency                 : {p99:.2f} ms")
    print(f"In-Process Call Throughput  : {throughput:,.1f} calls/sec")
    print(f"Idle RAM (RSS)              : {idle_rss:.1f} MB")
    print(f"Active RAM Usage (RSS)      : {active_rss:.1f} MB")
    print("-----------------------------------------")

    # Cleanup temporary database files
    for f in [bench_db, f"{bench_db}-wal", f"{bench_db}-shm"]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"[+] Wrote benchmark results to {output_path}")

    return results


def main():
    parser = argparse.ArgumentParser(description="SentinelGate In-Process Benchmark Suite")
    parser.add_argument("--output", type=str, default="benchmarks/results.json", help="Path to write JSON results")
    parser.add_argument("--requests", type=int, default=100, help="Number of benchmark queries")
    args = parser.parse_args()

    run_benchmark(output_path=args.output, num_requests=args.requests)


if __name__ == "__main__":
    main()
