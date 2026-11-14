"""Demonstrates local fastembed semantic caching in SentinelGate."""

import os
import time
import httpx

BASE_URL = os.getenv("SENTINELGATE_BASE_URL", "http://localhost:8000/v1")

# Two queries with the same intent phrased differently
QUERY_ORIGINAL = "What is quantum superposition?"
QUERY_PARAPHRASED = "Can you explain quantum superposition simply?"

def run_query(prompt: str, headers: dict = None) -> tuple[str, str, float]:
    start = time.perf_counter()
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)

    res = httpx.post(
        f"{BASE_URL}/chat/completions",
        headers=req_headers,
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=30.0,
    )
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    cache_status = res.headers.get("X-Cache", "MISS")
    content = res.json()["choices"][0]["message"]["content"]
    return content, cache_status, elapsed_ms

print("=== Step 1: Initial query (Cache Miss) ===")
_, status1, ms1 = run_query(QUERY_ORIGINAL)
print(f"Status: {status1} | Latency: {ms1:.2f} ms")

print("\n=== Step 2: Paraphrased query (Cache Hit via FastEmbed) ===")
_, status2, ms2 = run_query(QUERY_PARAPHRASED)
print(f"Status: {status2} | Latency: {ms2:.2f} ms")

print("\n=== Step 3: Bypass cache explicitly via X-No-Cache header ===")
_, status3, ms3 = run_query(QUERY_PARAPHRASED, headers={"X-No-Cache": "true"})
print(f"Status: {status3} | Latency: {ms3:.2f} ms")
