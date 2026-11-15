# SentinelGate Systems Performance Benchmarks

> **Methodology Notice:** All HTTP metrics below are captured using a real HTTP harness (`benchmarks/bench.py`) executing against an in-process Uvicorn gateway over loopback sockets (`127.0.0.1`) with `httpx.AsyncClient` connection pooling. Tail latency hedging metrics derive from a controlled mock provider simulation (`benchmarks/bench_hedging.py`). Every number below matches the raw committed JSON artifacts exactly.

### Test Runner & Environment
- **Git Commit SHA:** `68337eb6bd76812ef3c2d59b6a0c34799691e0fb`
- **Benchmark Date:** `2026-11-15 20:22 UTC`
- **Runner Environment:** Local Host (Darwin arm64)
- **Operating System:** `Darwin` (arm64)
- **Python Version:** `3.12.16`
- **Connection Pool:** `max_keepalive_connections=50, max_connections=100`

---

## 1. Raw Proxy Latency & Gateway Overhead

Measures raw HTTP round-trip latency when proxying cache-miss requests through SentinelGate compared against a direct mock upstream server returning instant completions.

| Endpoint Target | p50 (ms) | p95 (ms) | p99 (ms) | Min (ms) | Max (ms) | Throughput (req/s) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Direct Mock Upstream** | 0.71 | 2.63 | 3.12 | 0.58 | 3.14 | 1,110.5 |
| **SentinelGate Proxy** | 14.84 | 28.48 | 32.09 | 12.27 | 32.56 | 60.6 |
| **Net Gateway Overhead** | **+14.13** | **+25.85** | **+28.97** | **+11.69** | - | - |

---

## 2. Semantic Cache Hit Latency Across Concurrency

Measures semantic vector lookup and SQLite WAL cache retrieval latency under varying concurrent client workloads.

| Concurrency Level | p50 (ms) | p95 (ms) | p99 (ms) | Min (ms) | Max (ms) | Throughput (req/s) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Concurrency c = 1** | 1.88 | 3.70 | 5.82 | 1.75 | 6.44 | 427.1 |
| **Concurrency c = 10** | 16.80 | 46.77 | 56.46 | 5.98 | 61.63 | 460.5 |
| **Concurrency c = 50** | 60.10 | 98.30 | 102.19 | 22.06 | 104.61 | 450.0 |

---

## 3. Pre-Execution Agent Tool Firewall Inspection Overhead

Measures deterministic lexical parsing, AST analysis (`sqlglot`), and IP SSRF validation in `AgentToolGuard.inspect_tool_call`.

| Security Subsystem | p50 (ms) | p95 (ms) | p99 (ms) | Min (ms) | Max (ms) | Inspection Rate (calls/s) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Agent Tool & MCP Firewall** | **0.10** | **0.82** | **1.06** | 0.04 | 64.26 | **2,423.2** |

---

## 4. Speculative Latency Hedging Tail Latency Reduction

> **Simulation Parameters:** Modeled with a simulated mock provider exhibiting bimodal upstream latency (5.0% probability of ~2000ms delay vs 400ms baseline). Hedge triggered at SLA deadline `450ms`, racing primary against fast fallback (45ms).

| Execution Strategy | p50 (ms) | p90 (ms) | p95 (ms) | p99 (ms) |
| :--- | :---: | :---: | :---: | :---: |
| **Unhedged Baseline** | 400.9 | 418.6 | 497.9 | **2029.1** |
| **Speculative Hedged (`delay=450ms`)** | 402.7 | 419.9 | 424.2 | **507.1** |
| **Improvement** | - | - | - | **-75.0% Tail Latency** |

---

## Reproduction Commands

To reproduce these benchmarks on your local machine or CI runner:

```bash
# 1. Run live HTTP benchmark suite (<30 seconds)
uv run python benchmarks/bench.py --quick

# 2. Run speculative hedging simulation
uv run python benchmarks/bench_hedging.py

# 3. Regenerate this document strictly from committed JSON output
uv run python scripts/generate_benchmarks_doc.py
```
