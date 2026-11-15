<p align="center">
  <img src="docs/banner.svg" alt="SentinelGate Banner" width="100%">
</p>

<p align="center">
  <strong>Zero-bloat, production-grade OpenAI-compatible LLM gateway with in-process semantic caching, pre-execution agent firewall, stateful secret redaction, and sub-millisecond cost routing. Zero Redis. Zero PostgreSQL. Zero node_modules.</strong>
</p>

<p align="center">
  <a href="https://github.com/giridhar2005/sentinelgate/actions/workflows/ci.yml"><img src="https://github.com/giridhar2005/sentinelgate/actions/workflows/ci.yml/badge.svg" alt="CI Status"></a>
  <a href="https://github.com/giridhar2005/sentinelgate/actions/workflows/codeql.yml"><img src="https://github.com/giridhar2005/sentinelgate/actions/workflows/codeql.yml/badge.svg" alt="CodeQL Status"></a>
  <a href="https://github.com/giridhar2005/sentinelgate/releases"><img src="https://img.shields.io/github/v/release/giridhar2005/sentinelgate?style=flat-square&color=emerald" alt="Release"></a>
  <a href="BENCHMARKS.md"><img src="https://img.shields.io/badge/Benchmarks-p50_0.18ms-10B981.svg?style=flat-square" alt="Benchmarks"></a>
  <a href="ARCHITECTURE.md"><img src="https://img.shields.io/badge/RFC-Architecture_Doc-orange.svg?style=flat-square" alt="Architecture RFC"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=flat-square" alt="MIT License"></a>
</p>

<p align="center">
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.9%20%7C%203.10%20%7C%203.11%20%7C%203.12-3776AB.svg?style=flat-square&logo=python&logoColor=white" alt="Python 3.9+"></a>
  <a href="https://fastapi.tiangolo.com/"><img src="https://img.shields.io/badge/FastAPI-0.110+-009688.svg?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI"></a>
  <a href="https://www.sqlite.org/"><img src="https://img.shields.io/badge/SQLite-WAL_Mode-003B57.svg?style=flat-square&logo=sqlite&logoColor=white" alt="SQLite WAL"></a>
  <a href="https://prometheus.io/"><img src="https://img.shields.io/badge/Prometheus-RFC_0004_Metrics-E6522C.svg?style=flat-square&logo=prometheus&logoColor=white" alt="Prometheus"></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json" alt="Ruff"></a>
</p>

<p align="center">
  <a href="#hero-operational-console">Live Demo</a> &bull;
  <a href="#core-value-matrix">Value Matrix</a> &bull;
  <a href="#quickstart-in-10-seconds">10-Second Quickstart</a> &bull;
  <a href="#architecture-contrast-vs-alternatives">Architecture Contrast</a> &bull;
  <a href="#empirical-systems-benchmarks">Benchmarks</a> &bull;
  <a href="#agentic-tool-call--mcp-security-firewall">Agent Firewall</a> &bull;
  <a href="#sliding-window-sse-stream-redaction">Stream Redaction</a> &bull;
  <a href="ARCHITECTURE.md">Systems RFC</a>
</p>

<p align="center">
  <img src="docs/demo.gif" alt="SentinelGate Operational Console & Telemetry Demo" width="100%" style="border-radius: 8px; border: 1px solid #27272A; box-shadow: 0 8px 30px rgba(0,0,0,0.5);">
</p>

---

## Core Value Matrix

| 💰 Cost Reduction (-68%) | 🛡️ Agent & Secret Security | ⚡ Zero-Ops Reliability |
| :--- | :--- | :--- |
| **In-Process Semantic Cache**<br>Evaluates `BAAI/bge-small-en-v1.5` ONNX embeddings locally on CPU in `<2ms`. Redundant organizational queries resolve at **$0.00 token cost**. | **Pre-Execution Tool Firewall**<br>Intercepts destructive shell commands (`rm -rf`), SQL mutations (`DROP TABLE`), and SSRF metadata calls before client agents execute them. | **Single-Binary Topology**<br>Runs on SQLite WAL mode (`PRAGMA mmap_size=256MB`). Zero external Redis clusters, PostgreSQL daemons, or Celery workers. |
| **Microsecond Smart Router**<br>`model="auto"` evaluates syntax and reasoning density in `<0.2ms`, triaging utility queries to $0.15/1M models instead of $15/1M frontier models. | **Stateful Stream Redactor**<br>Maintains an async sliding-window lookahead buffer (N=64) on SSE `text/event-stream` chunks, catching split credentials with zero token leakage. | **Dynamic Circuit Breakers**<br>Provider outages or 429 rate limits automatically trigger failover to secondary models with zero dropped requests. |

---

## Quickstart in 10 Seconds

### Option A: Global CLI Execution (Zero-Key Sandbox Mode)
```bash
# Install globally via pip or uv
pip install sentinelgate

# Launch in deterministic sandbox mode (no API keys required)
sentinelgate --port 8000 --demo
```

### Option B: Ultra-Slim Docker One-Liner (<150MB)
```bash
docker run -d -p 8000:8000 --name sentinelgate ghcr.io/giridhar2005/sentinelgate:latest
```

Open **`http://localhost:8000/dashboard`** to access the linear obsidian developer HUD, inspect live traces, test agent attacks, and simulate cloud savings.

### 4-Line Drop-in Integration (OpenAI SDK)

SentinelGate is 100% wire-compatible with the standard OpenAI API:

```python
from openai import OpenAI

# 1. Point your client to SentinelGate
client = OpenAI(base_url="http://localhost:8000/v1", api_key="sentinel-local")

# 2. Execute requests with auto-routing, semantic caching, and secret redaction enabled
response = client.chat.completions.create(
    model="auto",  # Dynamically routes to cheapest model matching prompt complexity
    messages=[{"role": "user", "content": "Explain connection pooling in HTTP clients"}],
)
print(response.choices[0].message.content)
```

---

## Architecture Contrast vs. Alternatives

| Engineering Dimension | SentinelGate | LiteLLM Proxy | Portkey Gateway |
| :--- | :--- | :--- | :--- |
| **Operational Topology** | **Single Python Process** | Multi-service (Redis + Postgres + Celery) | Multi-container / Cloud SaaS |
| **External Dependencies** | **Zero (In-Process SQLite WAL + ONNX)** | Requires Redis cluster & PostgreSQL | Cloud DBs & external control plane |
| **Streaming Secret Redaction** | **Stateful Sliding-Window (N=64 char lookahead)** | Naive per-chunk (fails across token splits) | Not supported on stream |
| **Agent Tool & MCP Security** | **Pre-execution AST & regex firewall (Shell/SQL/SSRF)** | Post-hoc or uninspected | Basic policy checks |
| **Distributed Tracing** | **Zero-Dependency OTLP JSON + W3C `traceparent`** | Requires 50MB `opentelemetry-sdk` | Proprietary logging format |
| **Telemetry Pipeline** | **Non-blocking `asyncio.Queue` + WAL batch flusher** | Synchronous DB writes / Celery queue | Network telemetry egress |
| **Cold Start Time** | **< 1.2s** | 8.0s – 15.0s | N/A (Cloud Hosted) |
| **Base Memory (RSS)** | **~45 MB (base) / ~350 MB (with ONNX model)** | ~550 MB – 1.2 GB | Multi-container overhead |

<p align="center">
  <img src="docs/architecture-pillars.svg" alt="SentinelGate Core Architectural Pillars" width="100%" style="border-radius: 8px; border: 1px solid #27272A; margin: 12px 0;">
</p>

---

## Empirical Systems Benchmarks

Benchmarks recorded under 50-worker concurrent load testing (`benchmarks/load_test.py`):

| Architecture | Storage Engine | Network Hops | p50 Latency | p95 Latency | p99 Latency | Max Throughput (1 Core) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **SentinelGate** | **SQLite WAL (`mmap_size=30GB`)** | **0 (In-Process)** | **0.18 ms** | **0.34 ms** | **0.52 ms** | **14,200 req/sec** |
| Standard Gateway | Localhost Redis (TCP socket) | 1 (Localhost TCP) | 1.42 ms | 2.10 ms | 3.85 ms | 4,800 req/sec |
| Enterprise Gateway | Remote Managed Redis / Upstash | 1-2 (VPC / WAN) | 18.50 ms | 32.10 ms | 74.00 ms | 850 req/sec |
| RDBMS Gateway | PostgreSQL / MySQL | 1 (Connection pool) | 4.80 ms | 9.40 ms | 18.20 ms | 1,200 req/sec |

### Speculative Latency Hedging ("The Tail at Scale")

Production LLM APIs exhibit high p99 tail latency variance from GPU scheduling, queue delays, and KV cache recalculations.

SentinelGate implements **Speculative Latency Hedging**: if primary provider does not respond within `hedge_delay_ms=450`, SentinelGate speculatively dispatches a duplicate request to a designated fast fallback provider (e.g. Groq `llama-3.1-8b-instant`). Whichever finishes first is returned to the client; the slower request is aborted asynchronously.

| Execution Strategy | p50 Latency | p90 Latency | p95 Latency | p99 Latency (Tail) |
| :--- | :--- | :--- | :--- | :--- |
| Unhedged Requests | 412 ms | 890 ms | 1,450 ms | **2,480 ms** |
| **Speculative Hedged (`hedge_delay_ms=450`)** | **398 ms** | **445 ms** | **462 ms** | **485 ms** |
| **Tail Improvement** | **-3.4%** | **-50.0%** | **-68.1%** | **-80.4% Latency Slashed** |

---

## Agentic Tool-Call & MCP Security Firewall

Autonomous agents (Cursor, Claude Code, Cline, MCP servers) possess terminal and database execution capabilities. Traditional proxies forward model completions without analyzing tool calls, exposing local and cloud environments to catastrophic execution bugs.

SentinelGate executes deterministic **Pre-Execution Tool Inspection** on assistant `tool_calls` and `POST /v1/mcp/proxy` requests:

```
[Agent Assistant Output] ──> [Pre-Execution Guard] ──> [Threat Check: Pass] ──> [Client Agent Executes]
                                     │
                             [Threat Detected]
                                     │
                             ┌───────┴─────────────────────────────┐
                             ▼                                     ▼
                  [Shell / SQL / SSRF Blocked]         [Synthetic OpenAI Refusal]
                  (rm -rf, DROP TABLE, 169.254)         (Agent Safely Self-Corrects)
```

- **Shell Protection:** Blocks `rm -rf`, `curl | sh`, `wget | bash`, reverse shells, and SSH key exfiltration (`cat ~/.ssh/id_rsa`).
- **SQL Protection:** Intercepts unconstrained mutations (`DROP TABLE`, `TRUNCATE`, unguarded `DELETE FROM`).
- **SSRF Protection:** Halts outbound requests targeting cloud instance metadata (`169.254.169.254`, `metadata.google.internal`) and private RFC-1918 subnets.
- **Agent Self-Correction:** Returns structured OpenAI refusals with `finish_reason="stop"`, enabling agents to recover without crashing execution loops.

---

## Sliding-Window SSE Stream Redaction

Naive proxies inspect secrets either per full JSON payload or per individual SSE chunk. When streaming, tokens are fragmented arbitrarily by provider tokenizers:

```
Chunk 1: "{\"content\": \"sk-\"}"
Chunk 2: "{\"content\": \"proj-\"}"
Chunk 3: "{\"content\": \"998877665544332211\"}"
```

A per-chunk regex fails to detect `sk-proj-998877...` because the secret spans across chunk boundaries.

SentinelGate implements an async **Sliding-Window Lookahead Buffer** (`N=64` chars). Response text is held in an internal buffer until a safe emission boundary is reached (snapping to whitespace or punctuation). Fragmented API keys, JWTs, AWS credentials, and database URIs are recognized, redacted, and logged with sub-millisecond overhead and zero leakage.

---

## Distributed Tracing & Telemetry Architecture

- **W3C Traceparent Propagation:** SentinelGate parses incoming `traceparent` headers (`00-{trace_id}-{parent_id}-{flags}`) and generates compliant child spans forwarded to upstream LLMs.
- **Zero-Dependency OTLP JSON Exporter:** Emits OpenTelemetry-compliant JSON spans to collectors (Jaeger, Datadog, Grafana Tempo) without importing bulky third-party SDKs.
- **Non-Blocking Telemetry Queue:** The critical proxy path enqueues telemetry payloads into an in-memory `asyncio.Queue`. A dedicated background batch-worker drains the queue and writes records via SQLite WAL `executemany` batches, preventing disk I/O from introducing proxy latency.

---

## Project Structure & Engineering Deep-Dives

```
sentinelgate/
├── app/
│   ├── core/
│   │   ├── config.py              # Strict Pydantic v2 configuration schema
│   │   ├── database.py            # SQLite WAL mode initialization & pragmas
│   │   ├── security.py            # Reversible secret substitution & OpenAI errors
│   │   └── telemetry_otlp.py      # Zero-dependency OTLP JSON & W3C traceparent
│   ├── routers/
│   │   ├── dashboard.py           # Developer HUD, live benchmark runner, evals
│   │   ├── health.py              # /health & /v1/models inspection
│   │   ├── mcp.py                 # MCP (Model Context Protocol) security proxy
│   │   ├── metrics.py             # Prometheus RFC 0004 metrics exposition
│   │   └── v1.py                  # POST /v1/chat/completions critical path
│   ├── services/
│   │   ├── agent_guard.py         # Pre-execution AST & regex tool-call firewall
│   │   ├── budget.py              # Virtual API keys & dual-bucket rate limiters
│   │   ├── cache.py               # FastEmbed BAAI/bge-small-en-v1.5 semantic cache
│   │   ├── discovery.py           # Zero-config cloud key & local server discovery
│   │   ├── evals.py               # Non-blocking trace logging & shadow evals
│   │   ├── guardrails.py          # Context compaction & PII reversible masking
│   │   ├── router.py              # Speculative hedging, auto-router & circuit breaker
│   │   ├── streaming_redactor.py  # Sliding-window SSE stream secret redactor
│   │   └── telemetry_queue.py     # Non-blocking asyncio.Queue batch worker
│   └── main.py                    # Lifespan startup, ASCII banner, and app factory
├── benchmarks/
│   └── load_test.py               # 50-worker concurrent load testing harness
├── docs/
│   ├── ARCHITECTURE.md            # Deep-dive systems RFC and engineering tradeoffs
│   ├── BENCHMARKS.md              # Empirical throughput and latency profiles
│   ├── LAUNCH_PLAYBOOK.md         # Show HN, Twitter thread, and outreach templates
│   └── demo.gif                   # Automated 15-second visual walkthrough
├── scripts/
│   └── record_demo.py             # Automated Playwright recording harness
├── tests/                         # 60 unit, integration, and adversarial tests
└── pyproject.toml                 # Zero-bloat packaging (pipx/uvx CLI entrypoint)
```

For technical deep-dives into memory mapping, concurrency guarantees, and failover mechanics, read [ARCHITECTURE.md](docs/ARCHITECTURE.md) and [BENCHMARKS.md](docs/BENCHMARKS.md).

---

## Test Verification

The repository includes 60 automated unit, integration, and adversarial security tests:

```bash
# Run complete test suite
pytest -v

# Run Ruff linter and import check
ruff check app benchmarks tests
```

---

## About the Author

Built by **Bathini Venkata Giridhar** — Systems & AI Infrastructure Engineer specializing in high-throughput gateway runtimes, zero-dependency storage engines, and agentic security firewalls. Open to Founding AI Infrastructure, Staff Systems, and Backend Platform roles. Connect on GitHub: https://github.com/giridhar2005

---

## License

MIT License. See [LICENSE](LICENSE) for details.
