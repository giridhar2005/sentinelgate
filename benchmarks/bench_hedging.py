"""Speculative Latency Hedging Simulation Benchmark.

Measures p50, p90, p95, and p99 tail latency reduction when speculative request
hedging is enabled against a simulated primary provider with tail latency spikes.
Exports raw metrics to JSON for README generation.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import platform
import random
import sys
import time
from typing import Any, Dict, List

import numpy as np

# Add repository root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def compute_percentiles(latencies_ms: List[float]) -> Dict[str, float]:
    arr = np.array(latencies_ms)
    return {
        "p50": round(float(np.percentile(arr, 50)), 1),
        "p90": round(float(np.percentile(arr, 90)), 1),
        "p95": round(float(np.percentile(arr, 95)), 1),
        "p99": round(float(np.percentile(arr, 99)), 1),
        "min": round(float(np.min(arr)), 1),
        "max": round(float(np.max(arr)), 1),
    }


async def simulate_primary_provider(
    is_tail: bool, baseline_sec: float = 0.40, tail_sec: float = 2.48
) -> Dict[str, Any]:
    # Normal latency jitter around baseline (+/- 15ms)
    delay = (
        (tail_sec + random.uniform(-0.05, 0.05))
        if is_tail
        else (baseline_sec + random.uniform(-0.02, 0.02))
    )
    await asyncio.sleep(delay)
    return {"provider": "primary", "latency_sec": delay}


async def simulate_fallback_provider(fallback_sec: float = 0.045) -> Dict[str, Any]:
    delay = fallback_sec + random.uniform(-0.005, 0.010)
    await asyncio.sleep(delay)
    return {"provider": "fallback", "latency_sec": delay}


async def execute_simulated_hedged_request(is_tail: bool, hedge_delay_sec: float = 0.45) -> float:
    t0 = time.perf_counter()
    primary_task = asyncio.create_task(simulate_primary_provider(is_tail))
    done, _ = await asyncio.wait([primary_task], timeout=hedge_delay_sec)

    if primary_task in done:
        await primary_task
        return (time.perf_counter() - t0) * 1000.0

    # Primary exceeded deadline: trigger speculative hedge
    fallback_task = asyncio.create_task(simulate_fallback_provider())
    tasks = [primary_task, fallback_task]

    for coro in asyncio.as_completed(tasks):
        await coro
        break

    for t in tasks:
        if not t.done():
            t.cancel()

    return (time.perf_counter() - t0) * 1000.0


async def run_hedging_benchmark(
    num_requests: int = 100,
    tail_percentage: float = 10.0,
    hedge_delay_ms: float = 450.0,
    output_path: str = "benchmarks/hedging_results.json",
) -> Dict[str, Any]:
    print("==================================================")
    print("      Speculative Latency Hedging Benchmark       ")
    print("==================================================")
    print(f"Total Requests      : {num_requests}")
    print(f"Tail Probability    : {tail_percentage}% (delay ~2.48s vs 400ms baseline)")
    print(f"Hedge Delay (SLA)   : {hedge_delay_ms} ms")

    random.seed(42)
    # Generate distribution where tail_percentage of requests experience delay
    tail_flags = [
        True if i < int(num_requests * (tail_percentage / 100.0)) else False
        for i in range(num_requests)
    ]
    random.shuffle(tail_flags)

    # 1. Measure unhedged requests (concurrency=20 to finish in ~3 seconds)
    sem = asyncio.Semaphore(20)
    unhedged_latencies: List[float] = []

    async def run_unhedged(is_tail: bool):
        async with sem:
            t0 = time.perf_counter()
            await simulate_primary_provider(is_tail)
            unhedged_latencies.append((time.perf_counter() - t0) * 1000.0)

    print("\n[*] Running unhedged request baseline...")
    await asyncio.gather(*[run_unhedged(flag) for flag in tail_flags])

    # 2. Measure hedged requests
    hedged_latencies: List[float] = []

    async def run_hedged(is_tail: bool):
        async with sem:
            lat = await execute_simulated_hedged_request(
                is_tail, hedge_delay_sec=hedge_delay_ms / 1000.0
            )
            hedged_latencies.append(lat)

    print("[*] Running speculative hedged requests...")
    await asyncio.gather(*[run_hedged(flag) for flag in tail_flags])

    unhedged_stats = compute_percentiles(unhedged_latencies)
    hedged_stats = compute_percentiles(hedged_latencies)

    tail_reduction_pct = round(
        ((unhedged_stats["p99"] - hedged_stats["p99"]) / max(0.001, unhedged_stats["p99"])) * 100.0,
        1,
    )

    results: Dict[str, Any] = {
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        ),
        "methodology": "Simulated mock provider sweep measuring tail tolerance under bimodal upstream latency",
        "runner": {
            "os": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "is_ci": os.getenv("GITHUB_ACTIONS") == "true",
        },
        "config": {
            "total_requests": num_requests,
            "tail_percentage": tail_percentage,
            "primary_baseline_ms": 400.0,
            "primary_tail_spike_ms": 2480.0,
            "fallback_response_ms": 45.0,
            "hedge_delay_ms": hedge_delay_ms,
        },
        "unhedged": unhedged_stats,
        "hedged": hedged_stats,
        "tail_reduction_pct": tail_reduction_pct,
    }

    print("\n---------------- Results ----------------")
    print(
        f"Unhedged p50 / p90 / p95 / p99 : {unhedged_stats['p50']} / {unhedged_stats['p90']} / {unhedged_stats['p95']} / {unhedged_stats['p99']} ms"
    )
    print(
        f"Hedged   p50 / p90 / p95 / p99 : {hedged_stats['p50']} / {hedged_stats['p90']} / {hedged_stats['p95']} / {hedged_stats['p99']} ms"
    )
    print(f"Tail Latency Reduction (p99)   : -{tail_reduction_pct}%")
    print("-----------------------------------------")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"[+] Wrote hedging results to {output_path}")

    return results


def main():
    parser = argparse.ArgumentParser(
        description="SentinelGate Speculative Latency Hedging Benchmark"
    )
    parser.add_argument(
        "--output",
        type=str,
        default="benchmarks/hedging_results.json",
        help="Path to write JSON results",
    )
    parser.add_argument("--requests", type=int, default=100, help="Number of benchmark queries")
    args = parser.parse_args()

    asyncio.run(run_hedging_benchmark(output_path=args.output, num_requests=args.requests))


if __name__ == "__main__":
    main()
