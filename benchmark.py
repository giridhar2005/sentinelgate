from __future__ import annotations

import os
import random
import time

import httpx
import numpy as np
import respx
from starlette.testclient import TestClient

import main
from cache import SemanticCache
from evals import EvalsManager
from router import ProviderRouter


def run_benchmark():
    db_path = "benchmark_sentinelgate.db"
    if os.path.exists(db_path):
        os.remove(db_path)

    # 10 prompt archetypes with repetitions to model real semantic traffic clustering
    sample_queries = [
        "What is the capital of France?",
        "Tell me the capital of France.",
        "What city is the French capital?",
        "Explain Docker containers in 2 sentences.",
        "Briefly describe what a Docker container is.",
        "What is the Pythagorean theorem?",
        "Give me the formula for the Pythagorean theorem.",
        "How do I sort a list in Python?",
        "Write Python code to sort an array.",
        "My email is developer@sentinelgate.io, how to use the API?",
    ]

    total_requests = 100
    latencies = []
    cached_latencies = []
    uncached_latencies = []
    cache_hits = 0

    print(f"\n🚀 Running SentinelGate Benchmark across {total_requests} requests...")

    # Mock upstream provider with realistic LLM network/inference latency (180ms - 280ms)
    def mock_llm_upstream(request):
        time.sleep(random.uniform(0.18, 0.28))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-bench",
                "object": "chat.completion",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "Benchmark simulated completion response."},
                }],
                "usage": {"prompt_tokens": 16, "completion_tokens": 12, "total_tokens": 28},
            },
        )

    with respx.mock(assert_all_called=False) as mock:
        mock.post("https://api.openai.com/v1/chat/completions").mock(side_effect=mock_llm_upstream)
        mock.post("https://api.groq.com/openai/v1/chat/completions").mock(side_effect=mock_llm_upstream)
        mock.post("http://localhost:11434/v1/chat/completions").mock(side_effect=mock_llm_upstream)
        mock.post(url__startswith="https://generativelanguage.googleapis.com").mock(side_effect=mock_llm_upstream)

        # Isolated benchmark environment
        main.cache = SemanticCache(db_path=db_path, similarity_threshold=0.92)
        main.evals = EvalsManager(db_path=db_path)
        main.router = ProviderRouter(config_path="config.yaml")
        main.router.client = None

        client = TestClient(main.app)

        start_all = time.time()
        for i in range(total_requests):
            query = random.choice(sample_queries)
            t0 = time.time()
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": query}],
                },
            )
            duration_ms = (time.time() - t0) * 1000.0
            latencies.append(duration_ms)

            if resp.status_code == 200:
                is_hit = resp.json().get("sentinel_cache_hit", False)
                if is_hit:
                    cache_hits += 1
                    cached_latencies.append(duration_ms)
                else:
                    uncached_latencies.append(duration_ms)
            else:
                uncached_latencies.append(duration_ms)

        total_wall_time = time.time() - start_all

    # Telemetry metrics from gateway DB
    summary = main.evals.get_metrics_summary()

    p50 = float(np.percentile(latencies, 50))
    p90 = float(np.percentile(latencies, 90))
    p95 = float(np.percentile(latencies, 95))
    p99 = float(np.percentile(latencies, 99))

    avg_cached = float(np.mean(cached_latencies)) if cached_latencies else 0.0
    avg_uncached = float(np.mean(uncached_latencies)) if uncached_latencies else 0.0
    speedup = (avg_uncached / avg_cached) if avg_cached > 0 else 0.0
    hit_rate = (cache_hits / total_requests) * 100.0

    print("\n" + "=" * 65)
    print("           SENTINELGATE BENCHMARK RESULTS")
    print("=" * 65)
    print(f"Total Requests Processed : {total_requests}")
    print(f"Total Wall Clock Time    : {total_wall_time:.2f} s")
    print(f"Cache Hits               : {cache_hits} ({hit_rate:.1f}%)")
    print(f"Upstream LLM Calls       : {total_requests - cache_hits}")
    print(f"Avg Uncached Latency     : {avg_uncached:.2f} ms")
    print(f"Avg Cached Latency       : {avg_cached:.2f} ms")
    print(f"Cache Latency Speedup    : {speedup:.1f}x faster")
    print(f"Total USD Spent          : ${summary['total_cost_usd']:.5f}")
    print(f"Total USD Saved          : ${summary['total_saved_usd']:.5f}")
    print(f"P50 Latency              : {p50:.2f} ms")
    print(f"P90 Latency              : {p90:.2f} ms")
    print(f"P95 Latency              : {p95:.2f} ms")
    print(f"P99 Latency              : {p99:.2f} ms")
    print("=" * 65)

    md_table = f"""| Metric | Value |
|---|---|
| **Total Test Requests** | {total_requests} |
| **Cache Hit Rate** | {hit_rate:.1f}% ({cache_hits}/{total_requests}) |
| **Average Uncached Latency (Upstream LLM)** | {avg_uncached:.2f} ms |
| **Average Semantic Cache Latency** | {avg_cached:.2f} ms |
| **Semantic Cache Speedup** | **{speedup:.1f}x faster** |
| **Latency P50** | {p50:.2f} ms |
| **Latency P90** | {p90:.2f} ms |
| **Latency P95** | {p95:.2f} ms |
| **Latency P99** | {p99:.2f} ms |
| **Cost Incurred** | ${summary['total_cost_usd']:.5f} |
| **Cost Saved (via Cache)** | **${summary['total_saved_usd']:.5f}** |"""

    print("\n### Copy-Paste Markdown Table for README.md:\n")
    print(md_table)
    print()

    # Clean up benchmark db
    if os.path.exists(db_path):
        os.remove(db_path)

    return md_table


if __name__ == "__main__":
    run_benchmark()
