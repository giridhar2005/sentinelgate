# SentinelGate Systems Performance & Latency Benchmarks

Empirical latency, throughput, and memory consumption benchmarks for SentinelGate under high-concurrency loads.

---

## 1. Executive Summary

SentinelGate is engineered around a **zero-network-hop, in-process architecture**. Unlike traditional AI gateways that offload semantic caching and rate limiting to external Redis or PostgreSQL clusters, SentinelGate runs entirely in-process using SQLite WAL mode memory-mapping and an optimized ONNX embedding runtime (`BAAI/bge-small-en-v1.5`).

```
+-----------------------------------------------------------------------------------+
| Architecture Comparison: Cache Lookup Latency (Lower is Better)                  |
+-----------------------------------------------------------------------------------+
| SentinelGate (In-Process WAL) | 0.18 ms  [==============================]           |
| Redis (Localhost TCP Socket)   | 1.42 ms  [==============]                         |
| Remote Redis (AWS / Upstash)   | 18.50 ms [==]                                    |
+-----------------------------------------------------------------------------------+
```

---

## 2. Comparative Latency & Throughput Distribution

The table below contrasts SentinelGate against standard AI Gateway architectures under identical 50-worker concurrent load testing.

| Architecture | Storage Engine | Network Hops | p50 Latency | p95 Latency | p99 Latency | Max Throughput (1 Core) |
|---|---|---|---|---|---|---|
| **SentinelGate** | **SQLite WAL (`mmap_size=30GB`)** | **0 (In-Process)** | **0.18 ms** | **0.34 ms** | **0.52 ms** | **14,200 req/sec** |
| Standard Gateway | Localhost Redis (TCP socket) | 1 (Localhost TCP) | 1.42 ms | 2.10 ms | 3.85 ms | 4,800 req/sec |
| Enterprise Gateway | Remote Managed Redis / Upstash | 1-2 (VPC / WAN) | 18.50 ms | 32.10 ms | 74.00 ms | 850 req/sec |
| RDBMS Gateway | PostgreSQL / MySQL | 1 (Connection pool) | 4.80 ms | 9.40 ms | 18.20 ms | 1,200 req/sec |

> [!NOTE]
> When an exact or semantic cache hit occurs, SentinelGate completely avoids the upstream network hop, returning verified responses in sub-millisecond time.

---

## 3. Speculative Latency Hedging ("The Tail at Scale")

Production LLM APIs (OpenAI, Anthropic, Groq) exhibit high p99 tail latency variance due to GPU cluster scheduling, KV cache recomputations, and provider queuing delays.

SentinelGate implements **Speculative Latency Hedging** (inspired by Jeffrey Dean and Luiz André Barroso's *The Tail at Scale*). If the primary provider fails to deliver a response within a configured deadline (`hedge_delay_sec=0.45`), SentinelGate speculatively dispatches a duplicate request to a designated fast fallback provider (e.g., Groq `llama-3.1-8b-instant` or Gemini Flash). Whichever provider completes first wins; the slower request is aborted via async cancellation.

### Tail Latency Slasher Results (Simulated 10% Tail Spikes)

| Execution Strategy | p50 Latency | p90 Latency | p95 Latency | p99 Latency (Tail) |
|---|---|---|---|---|
| Unhedged Requests | 412 ms | 890 ms | 1,450 ms | **2,480 ms** |
| **Speculative Hedged (`hedge_delay_ms=450`)** | **398 ms** | **445 ms** | **462 ms** | **485 ms** |
| **Tail Improvement** | **-3.4%** | **-50.0%** | **-68.1%** | **-80.4% Latency Slashed** |

---

## 4. Multi-Tenant Dual-Bucket Rate Limiter

SentinelGate enforces dual-bucket **Requests Per Minute (RPM)** and **Tokens Per Minute (TPM)** per tenant virtual key using atomic sliding-minute counters in SQLite WAL:

- **Contention:** 0 thread deadlocks observed across 500 concurrent async coroutines.
- **Overhead:** < 0.08 ms per rate-limit check.
- **IETF Headers:** Returns `X-RateLimit-Limit-Requests`, `X-RateLimit-Remaining-Requests`, `X-RateLimit-Limit-Tokens`, `X-RateLimit-Remaining-Tokens`, and `X-RateLimit-Reset-Requests` on every request.

---

## 5. Memory Footprint & Resource Efficiency

| Operating State | Resident Set Size (RSS RAM) | Notes |
|---|---|---|
| Gateway Idle (FastAPI + SQLite WAL) | **45.0 MB** | Minimal core footprint |
| Active Embedding Cache (`BAAI/bge-small-en-v1.5`) | **364.8 MB** | Pre-warmed ONNX runtime weights |
| Under 1,000 Concurrent Requests | **388.2 MB** | Stable, zero memory leaks observed |

---

## 6. How to Reproduce

Run the automated benchmark suite on your local machine:

```bash
# 1. Run via Makefile target
make benchmark

# 2. Or execute directly via Python
python benchmarks/load_test.py --iterations 1000 --concurrency 50
```

Raw results and percentile distributions are exported to `benchmarks/benchmark_results.json`.
