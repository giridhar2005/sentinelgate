# SentinelGate Systems Performance Benchmarks

> **Methodology Notice:** All HTTP metrics below are captured using a real HTTP harness (`benchmarks/bench.py`) executing against an in-process Uvicorn gateway over loopback sockets (`127.0.0.1`) with `httpx.AsyncClient` connection pooling. Tail latency hedging metrics derive from a controlled mock provider simulation (`benchmarks/bench_hedging.py`). Every number below matches the raw committed JSON artifacts exactly.

### Test Runner & Environment
- **Git Commit SHA:** `5407a4c42fbb7f31b5d28d4b353d8944d5e0af97`
- **Benchmark Date:** `2026-11-15 20:13 UTC`
- **Runner Environment:** Local Host (Darwin arm64)
- **Operating System:** `Darwin` (arm64)
- **Python Version:** `3.12.16`
- **Connection Pool:** `max_keepalive_connections=50, max_connections=100`

---

## 1. Raw Proxy Latency & Gateway Overhead

Measures raw HTTP round-trip latency when proxying cache-miss requests through SentinelGate compared against a direct mock upstream server returning instant completions.

| Endpoint Target | p50 (ms) | p95 (ms) | p99 (ms) | Min (ms) | Max (ms) | Throughput (req/s) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Direct Mock Upstream** | 0.63 | 1.16 | 1.30 | 0.58 | 1.34 | 1,395.3 |
| **SentinelGate Proxy** | 7.52 | 9.37 | 13.32 | 7.13 | 14.51 | 125.3 |
| **Net Gateway Overhead** | **+6.89** | **+8.21** | **+12.02** | **+6.55** | - | - |

---

## 2. Semantic Cache Hit Latency Across Concurrency

Measures semantic vector lookup and SQLite WAL cache retrieval latency under varying concurrent client workloads.

| Concurrency Level | p50 (ms) | p95 (ms) | p99 (ms) | Min (ms) | Max (ms) | Throughput (req/s) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Concurrency c = 1** | 1.69 | 3.01 | 14.27 | 1.59 | 17.80 | 391.3 |
| **Concurrency c = 10** | 14.79 | 28.87 | 46.76 | 5.33 | 57.29 | 564.0 |
| **Concurrency c = 50** | 53.16 | 93.16 | 95.62 | 18.86 | 96.59 | 504.1 |

---

## 3. Pre-Execution Agent Tool Firewall Inspection Overhead

Measures deterministic lexical parsing, AST analysis (`sqlglot`), and IP SSRF validation in `AgentToolGuard.inspect_tool_call`.

| Security Subsystem | p50 (ms) | p95 (ms) | p99 (ms) | Min (ms) | Max (ms) | Inspection Rate (calls/s) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Agent Tool & MCP Firewall** | **0.10** | **1.15** | **1.37** | 0.04 | 22.72 | **2,614.5** |

---

## 4. Speculative Latency Hedging Tail Latency Reduction

> **Simulation Parameters:** Modeled with a simulated mock provider exhibiting bimodal upstream latency (5.0% probability of ~2000ms delay vs 400ms baseline). Hedge triggered at SLA deadline `450ms`, racing primary against fast fallback (45ms).

| Execution Strategy | p50 (ms) | p90 (ms) | p95 (ms) | p99 (ms) |
| :--- | :---: | :---: | :---: | :---: |
| **Unhedged Baseline** | 400.9 | 418.5 | 497.9 | **2029.2** |
| **Speculative Hedged (`delay=450ms`)** | 402.7 | 420.0 | 424.3 | **505.7** |
| **Improvement** | - | - | - | **-75.1% Tail Latency** |

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
