# SentinelGate

**A lightweight, single-process LLM gateway with in-process semantic caching, deterministic tool guardrails, and speculative latency hedging.**

<p align="center">
  <a href="https://github.com/giridhar2005/sentinelgate/actions/workflows/ci.yml"><img src="https://github.com/giridhar2005/sentinelgate/actions/workflows/ci.yml/badge.svg" alt="CI Status"></a>
  <a href="https://github.com/giridhar2005/sentinelgate/actions/workflows/codeql.yml"><img src="https://github.com/giridhar2005/sentinelgate/actions/workflows/codeql.yml/badge.svg" alt="CodeQL Status"></a>
  <a href="tests/"><img src="https://img.shields.io/badge/Coverage-87%25-brightgreen.svg?style=flat-square" alt="Test Coverage 87%"></a>
  <a href="https://github.com/giridhar2005/sentinelgate/releases"><img src="https://img.shields.io/github/v/release/giridhar2005/sentinelgate?style=flat-square&color=blue&label=release" alt="Release"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.10%20%7C%203.11%20%7C%203.12-3776AB.svg?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+"></a>
  <a href="https://fastapi.tiangolo.com/"><img src="https://img.shields.io/badge/FastAPI-0.110+-009688.svg?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI"></a>
  <a href="https://www.sqlite.org/"><img src="https://img.shields.io/badge/SQLite-WAL_Mode-003B57.svg?style=flat-square&logo=sqlite&logoColor=white" alt="SQLite WAL"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=flat-square" alt="MIT License"></a>
</p>

<p align="center">
  <img src="docs/demo.gif" alt="SentinelGate Operational Console & Telemetry Demo" width="100%" style="border-radius: 8px; border: 1px solid #27272A;">
</p>

---

## What is SentinelGate?

SentinelGate is an OpenAI- and Anthropic-compatible reverse proxy and LLM gateway written in Python (FastAPI, SQLite WAL, ONNX runtime). It runs as a single embedded process with zero external infrastructure dependencies (no Redis, no PostgreSQL, no Celery workers).

It provides local semantic caching using CPU embeddings, cost- and complexity-aware model routing (`model="auto"`), deterministic agent tool-call inspection, sliding-window streaming credential redaction, and speculative latency hedging.

## Who is it for?

- **Local Agent Developers:** Builders running coding agents (Cursor, Cline, Claude Code, OpenHands) who want local semantic caching, request observability, and token cost controls on a single machine without maintaining Redis or PostgreSQL containers.
- **Small Teams & Internal Platforms:** Engineering teams deploying an internal LLM gateway on a single host or VM that requires a minimal operational footprint.
- **Embedded & Edge Environments:** Systems requiring an embedded gateway with local vector similarity search running directly on CPU via ONNX models.

## Status: Alpha (v0.1.0)

SentinelGate is under active alpha development. Core proxying, semantic caching, and guardrail features are tested and verified in CI, but configuration schemas and internal APIs may evolve before v1.0.0.

> [!NOTE]
> Tool-call guardrails provide deterministic application-layer validation on model responses; they do not provide OS-level process sandboxing. See [docs/SECURITY.md](docs/SECURITY.md) for details on the security model and defense-in-depth recommendations. Full performance benchmarks are documented in [BENCHMARKS.md](BENCHMARKS.md).

---

## Quickstart

### 1. Run the Gateway

```bash
# Clone the repository
git clone https://github.com/giridhar2005/sentinelgate.git && cd sentinelgate

# Install dependencies and CLI
pip install .

# Start the gateway in demo sandbox mode (bound to 127.0.0.1)
sentinelgate --port 8000 --demo
```

Open `http://localhost:8000/dashboard` in your browser to inspect live metrics, traces, and test agent attacks.

### 2. Connect Your Client

Point any OpenAI-compatible client to `http://localhost:8000/v1`:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="admin-secret-token")

response = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Explain connection pooling in HTTP clients"}],
)
print(response.choices[0].message.content)
```

Or make a direct HTTP request:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer admin-secret-token" \
  -H "Content-Type: application/json" \
  -d '{"model": "auto", "messages": [{"role": "user", "content": "Ping"}]}'
```

For Claude Code CLI, Cursor, Cline, and Ollama setups, see [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md).

---

## Core Capabilities

- **In-Process Semantic Caching:** Embeds incoming prompts using local CPU ONNX (`BAAI/bge-small-en-v1.5`) and evaluates cosine similarity against memory-mapped SQLite cache entries. Repeated or semantically similar prompts resolve in under 5ms with zero upstream token cost.
- **Cost & Complexity Auto-Routing (`model="auto"`):** Evaluates prompt syntax, code tokens, and reasoning indicators in `<0.5ms` to route routine utility queries to lightweight models, reserving complex prompts for frontier reasoning models.
- **Deterministic Tool Firewall:** Scans `tool_calls` parameters in model responses using `shlex` lexical parsing for destructive shell commands (`rm -rf`, pipe-to-sh), `sqlglot` AST parsing for destructive SQL mutations (`DROP TABLE`, unconstrained `DELETE`), and IP resolution checks for SSRF destinations before returning payloads to client agents.
- **Streaming Secret Redaction:** Uses a sliding-window lookahead buffer across server-sent events (`text/event-stream`) to redact API keys and secrets split across arbitrary chunk boundaries.
- **Speculative Latency Hedging:** Dispatches a fallback request to a fast secondary provider if the primary does not respond within a configurable deadline (`hedge_delay_ms=450`), mitigating tail latency variance. Loser tasks are canceled immediately.
- **Dual API Compatibility:** Exposes both OpenAI-compatible `/v1/chat/completions` and Anthropic Messages `/v1/messages` endpoints with full SSE event mapping.
- **Embedded Single-Process Runtime:** Uses SQLite WAL mode (`PRAGMA mmap_size=256MB`) for virtual keys (hashed at rest with server pepper), dual-bucket rate limiting (RPM/TPM), and telemetry with zero external services.

---

## Repository Architecture

```
sentinelgate/
├── cli.py                     # Entrypoint CLI ('sentinelgate' command)
├── core/
│   ├── circuit.py             # Provider circuit breakers
│   ├── config.py              # Pydantic v2 configuration schema
│   ├── database.py            # SQLite WAL state engine
│   ├── security.py            # Constant-time auth & hashed virtual keys
│   └── telemetry_otlp.py      # Non-blocking OTLP trace context
├── routers/
│   ├── dashboard.py           # Obsidian HUD dashboard router
│   ├── dashboard_api.py       # HUD stats, chaos, and export endpoints
│   ├── health.py              # Health check endpoint
│   ├── mcp.py                 # Model Context Protocol reverse proxy
│   ├── metrics.py             # Prometheus RFC 0004 text exposition
│   └── v1.py                  # /v1/chat/completions & /v1/messages
├── services/
│   ├── agent_guard/           # Tool-call & MCP firewall (shell, sql, ssrf)
│   ├── budget.py              # Daily budget limits
│   ├── cache.py               # In-process semantic cache (FastEmbed ONNX)
│   ├── discovery.py           # Local models & provider auto-discovery
│   ├── evals.py               # Background parity evals
│   ├── gateway_pipeline.py    # Unified request execution pipeline
│   ├── guardrails.py          # PII masking & prompt injection inspector
│   ├── router/                # Smart router, hedging, failover, providers
│   ├── streaming_redactor.py  # Sliding-window streaming secret redactor
│   └── telemetry_queue.py     # Asynchronous batch trace queue
├── static/                    # Dashboard CSS and JS assets
└── templates/                 # Obsidian HUD HTML template
```

---

## Performance & Telemetry

All performance metrics below derive from real HTTP benchmarking over loopback sockets (`benchmarks/bench.py`) and reproducible simulation runs (`benchmarks/bench_hedging.py`). Committed raw telemetry is recorded in [`benchmarks/results.json`](benchmarks/results.json) and [`benchmarks/hedging_results.json`](benchmarks/hedging_results.json). See [BENCHMARKS.md](BENCHMARKS.md) for full reproduction commands and methodology details.

### Live Gateway Latency & Throughput

| Systems Dimension | Measured Telemetry | Benchmark Methodology |
| :--- | :--- | :--- |
| **Test Suite Verification** | **216 / 216 Tests Passed (87% Coverage)** | `pytest -v --cov=sentinelgate` |
| **Direct Mock Upstream Latency** | **0.63 ms** (p50) / **1.30 ms** (p99) | Raw in-process ASGI mock provider (1,395 req/s) |
| **Gateway Proxy Latency (Cache Miss)** | **7.52 ms** (p50) / **13.32 ms** (p99) | Full pipeline proxy with auth & telemetry |
| **Net Gateway Overhead** | **+6.89 ms** (p50) / **+12.02 ms** (p99) | Gateway end-to-end minus mock baseline |
| **Semantic Cache Hit (c=1)** | **1.69 ms** (p50) / **14.27 ms** (p99) | In-process SQLite WAL + FastEmbed ONNX (391 req/s) |
| **Semantic Cache Hit (c=10)** | **14.79 ms** (p50) / **46.76 ms** (p99) | Concurrent client pool (564 req/s) |
| **Semantic Cache Hit (c=50)** | **53.16 ms** (p50) / **95.62 ms** (p99) | High-concurrency client pool (504 req/s) |
| **Agent Tool Firewall Inspection** | **0.10 ms** (p50) / **1.37 ms** (p99) | Lexical AST parsing & SSRF validation (2,614 calls/s) |

### Speculative Latency Hedging (Simulated Upstream)

Production LLM endpoints frequently exhibit tail latency spikes from queue depth and GPU scheduling. SentinelGate implements speculative latency hedging: if the primary provider does not respond within `hedge_delay_ms=450`, a secondary request is dispatched to a fast fallback provider. Whichever completes first is returned.

| Execution Strategy | p50 Latency | p90 Latency | p95 Latency | p99 Latency (Tail) |
| :--- | :---: | :---: | :---: | :---: |
| Simulated Unhedged Requests | 400.9 ms | 418.5 ms | 497.9 ms | **2029.2 ms** |
| **Speculative Hedged (`hedge_delay_ms=450.0`)** | **402.7 ms** | **420.0 ms** | **424.3 ms** | **505.7 ms** |
| **Improvement** | - | - | - | **-75.1% Latency Reduction** |

*Methodology: Simulated mock provider benchmark (`benchmarks/bench_hedging.py`) modeling bimodal upstream latency (5.0% probability of ~2000ms delay vs 400ms baseline, 450ms hedge deadline, 45ms fast fallback).*

---

## Architectural Trade-Offs

SentinelGate is built as an embedded, single-process runtime. Full-featured gateways like [LiteLLM Proxy](https://docs.litellm.ai/docs/proxy/architecture) and [Portkey AI Gateway](https://docs.portkey.ai/docs/product/ai-gateway) offer broader multi-cloud provider ecosystems and enterprise control planes:

| Engineering Dimension | SentinelGate | LiteLLM Proxy ([Docs](https://docs.litellm.ai/docs/proxy/architecture)) | Portkey AI Gateway ([Docs](https://docs.portkey.ai/docs/product/ai-gateway)) |
| :--- | :--- | :--- | :--- |
| **Primary Architecture** | **Single-process embedded runtime** (FastAPI + SQLite WAL) | Standalone Python proxy or distributed multi-instance service | Open-source TypeScript proxy runtime or managed cloud SaaS |
| **External Dependencies** | **Zero** (In-process SQLite WAL + local ONNX runtime) | Zero for standalone CLI; Redis/Postgres optional for cluster state | Zero for standalone container; cloud DBs for control plane |
| **Provider Support** | Focused OpenAI-compatible backends (OpenAI, Anthropic, Groq, Ollama) | **100+ LLM providers** (Bedrock, Vertex AI, Azure, Cohere, etc.) | **250+ LLM providers** with unified multi-region routing |
| **Agent Tool & MCP Guardrail** | Built-in lexical `shlex` and `sqlglot` AST tool-call inspector | Pluggable third-party integrations (Presidio, Lakera, Aporia) | Configurable guardrails and enterprise policy rules |
| **Streaming Secret Redaction** | Stateful sliding window buffer lookahead | Middleware & callback hooks; Presidio PII plugins | Config-driven PII redaction and enterprise privacy rules |
| **Distributed Tracing** | Zero-dependency W3C `traceparent` parsing + raw OTLP JSON | OpenTelemetry SDK, Prometheus, Datadog, Langfuse | Built-in trace analytics, OpenTelemetry exporter |
| **State & Persistence** | Local SQLite database with Write-Ahead Logging (`WAL`) | Memory (single-node) or distributed Redis + PostgreSQL | Local SQLite/Redis or managed multi-region datastores |
| **Best Suited For** | **Single-host deployments, local coding agents, zero-infra edge proxies** | **Enterprise teams needing 100+ provider mappings and team billing** | **Production web apps needing hosted control planes and prompt governance** |

---

## Documentation

- **[Performance Benchmarks (Detailed Report)](BENCHMARKS.md):** Complete methodology, concurrency breakdown, and reproduction commands.
- **[Security & Threat Model](docs/SECURITY.md):** Threat boundaries, test corpus evaluation, evasion analysis, and disclosure policy.
- **[System Architecture (RFC)](ARCHITECTURE.md):** In-process request lifecycle, SQLite WAL storage model, and concurrency design.
- **[Client & IDE Integrations](docs/INTEGRATIONS.md):** Setup instructions for Cursor, Cline, Claude Code CLI, Ollama, and SDKs.
- **[Configuration Reference](docs/CONFIGURATION.md):** Configuration schema, rate limits, virtual keys, and routing options.

---

## Roadmap

- **v0.1.0 (Current):** Single-process gateway, OpenAI `/v1/chat/completions` and Anthropic `/v1/messages` compatibility, in-process FastEmbed ONNX caching, deterministic tool guardrails, speculative latency hedging, local SQLite WAL state.
- **v0.2.0:** Tree-sitter AST parsing for multi-language tool-call validation, pluggable vector index backends for large cache indices.
- **v0.3.0:** Raft replication for multi-instance SQLite WAL clustering without external database dependencies.
- **v1.0.0:** Stable public configuration schemas, plugin architecture, and native gRPC streaming.

---

## Developer Console Shortcuts

When using the `/dashboard` developer console, the following keyboard shortcuts are available:

| Shortcut | View | Description |
| :---: | :--- | :--- |
| <kbd>?</kbd> | **Help** | Toggle keyboard shortcut reference modal |
| <kbd>T</kbd> | **Traces** | Open live request trace stream |
| <kbd>F</kbd> | **Firewall** | Open agent firewall threat audit log |
| <kbd>B</kbd> | **Benchmarks** | Run in-process gateway benchmark |
| <kbd>R</kbd> | **Cost Model** | View estimated cost savings model |
| <kbd>Esc</kbd> | **Close** | Dismiss active drawer or modal |

---

## License

This project is licensed under the [MIT License](LICENSE).
