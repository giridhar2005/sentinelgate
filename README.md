# SentinelGate

**A lightweight, single-process LLM gateway with in-process semantic caching, heuristic tool guardrails, and speculative latency hedging.**

<p align="center">
  <a href="https://github.com/giridhar2005/sentinelgate/actions/workflows/ci.yml"><img src="https://github.com/giridhar2005/sentinelgate/actions/workflows/ci.yml/badge.svg" alt="CI Status"></a>
  <a href="https://github.com/giridhar2005/sentinelgate/actions/workflows/codeql.yml"><img src="https://github.com/giridhar2005/sentinelgate/actions/workflows/codeql.yml/badge.svg" alt="CodeQL Status"></a>
  <a href="tests/"><img src="https://img.shields.io/badge/Coverage-81%25-brightgreen.svg?style=flat-square" alt="Test Coverage 81%"></a>
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

It provides local semantic caching using CPU embeddings, cost- and complexity-aware model routing (`model="auto"`), heuristic agent tool-call inspection, sliding-window streaming credential redaction, and speculative latency hedging.

## Who is it for?

- **Local Agent Developers:** Builders running coding agents (Cursor, Cline, Claude Code, OpenHands) who want local semantic caching, request observability, and token cost controls on a single machine without maintaining Redis or PostgreSQL containers.
- **Small Teams & Internal Platforms:** Engineering teams deploying an internal LLM gateway on a single host or VM that requires a minimal operational footprint.
- **Embedded & Edge Environments:** Systems requiring an embedded gateway with local vector similarity search running directly on CPU via ONNX models.

## Status: Alpha (v0.5.0)

SentinelGate is under active alpha development. Core proxying, semantic caching, and guardrail features are tested and verified in CI, but configuration schemas and internal APIs may evolve before v1.0.0.

> [!NOTE]
> Tool-call guardrails are heuristic inspection filters on model response payloads; they do not provide OS-level process sandboxing. See [docs/SECURITY.md](docs/SECURITY.md) for details on the security model and known evasion boundaries.

---

## Quickstart

### 1. Run the Gateway

```bash
# Clone the repository
git clone https://github.com/giridhar2005/sentinelgate.git && cd sentinelgate

# Install dependencies and CLI (PyPI package pending v0.5.0 release)
pip install .

# Start the gateway in demo sandbox mode
sentinelgate --port 8000 --demo
```

Open `http://localhost:8000/dashboard` in your browser to inspect live metrics, traces, and test agent attacks.

### 2. Connect Your Client

Point any OpenAI-compatible client to `http://localhost:8000/v1`:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="sentinel-local")

response = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Explain connection pooling in HTTP clients"}],
)
print(response.choices[0].message.content)
```

Or make a direct HTTP request:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sentinel-local" \
  -H "Content-Type: application/json" \
  -d '{"model": "auto", "messages": [{"role": "user", "content": "Ping"}]}'
```

For Claude Code CLI, Cursor, Cline, and Ollama setups, see [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md).

---

## Core Capabilities

- **In-Process Semantic Caching:** Embeds incoming prompts using local CPU ONNX (`BAAI/bge-small-en-v1.5`) and evaluates cosine similarity against memory-mapped SQLite cache entries. Repeated or semantically similar prompts resolve in under 5ms with zero upstream token cost.
- **Cost & Complexity Auto-Routing (`model="auto"`):** Evaluates prompt syntax, code tokens, and reasoning indicators in `<0.5ms` to route routine utility queries to lightweight models, reserving complex prompts for frontier reasoning models.
- **Heuristic Tool Guardrails:** Scans `tool_calls` parameters in model responses for common dangerous shell patterns (`rm -rf`, pipe-to-sh), destructive SQL mutations (`DROP TABLE`, unconstrained `DELETE`), and SSRF destinations before returning payloads to client agents.
- **Streaming Secret Redaction:** Uses a sliding-window lookahead buffer ($N=64$ chars) across server-sent events (`text/event-stream`) to redact API keys and secrets split across arbitrary chunk boundaries.
- **Speculative Latency Hedging:** Dispatches a fallback request to a fast secondary provider if the primary does not respond within a configurable deadline (`hedge_delay_ms=450`), mitigating tail latency variance.
- **Dual API Compatibility:** Exposes both OpenAI-compatible `/v1/chat/completions` and Anthropic Messages `/v1/messages` endpoints for drop-in compatibility.
- **Embedded Single-Process Runtime:** Uses SQLite WAL mode (`PRAGMA mmap_size=256MB`) for virtual keys, dual-bucket rate limiting (RPM/TPM), and telemetry with zero external services.

---

## Limitations & Threat Model

SentinelGate is designed for operational efficiency and defense-in-depth, but has explicit boundaries:

- **Guardrail Layer, Not a Security Sandbox:** The tool firewall heuristically inspects tool-call payloads in model completions. It cannot prevent a compromised client from executing malicious commands, and it cannot reliably detect obfuscated shell commands (e.g. base64 decoding, variable expansion, subshells) or complex SQL dialect evasions. Always run untrusted agents in containerized or microVM sandboxes (Docker, gVisor, Firecracker). See [docs/SECURITY.md](docs/SECURITY.md) for full adversarial test coverage and threat boundaries.
- **Single-Host State:** SQLite WAL mode provides concurrent reads and serialized writes on a single host. Virtual keys, rate limits, and cache entries do not replicate across multiple hosts without external synchronization.
- **CPU Embedding Latency:** FastEmbed ONNX executes on CPU without GPU acceleration. Generating embeddings takes ~2–5ms per unique prompt, which adds a small fixed latency overhead to uncached requests.
- **Pattern-Based Credential Detection:** Streaming redaction relies on regex and Shannon entropy heuristics. Highly fragmented tokens, uncommon custom secret formats, or encrypted payloads may not be detected.

---

## Performance & Telemetry

Benchmarks are executed automatically by GitHub Actions on every commit to `main`, and results are committed to [`benchmarks/results.json`](benchmarks/results.json).

<!-- BENCHMARK:START -->
| Systems Performance Dimension | Measured Telemetry | Protocol & Environment |
| :--- | :--- | :--- |
| **Test Suite Verification** | **76 / 76 Tests Passed (81% Coverage)** | `pytest -v --cov=app --cov=sentinelgate` |
| **In-Process Cache Overhead (Min)** | **1.48 ms** | Memory-mapped SQLite WAL (`PRAGMA mmap_size=256MB`) |
| **Semantic Cache Latency (Avg)** | **2.87 ms** | Local `BAAI/bge-small-en-v1.5` ONNX runtime on CPU |
| **Latency p50 / p95 / p99** | **2.47 ms / 29.47 ms / 31.70 ms** | Sequential query stream (10 unique semantic pairs) |
| **Cache Hit Efficiency** | **91.0%** (91/100 queries) | Exact + semantic cosine similarity threshold |
| **In-Process Call Throughput (No Network Hops)** | **179.5 calls/sec** | In-process ASGI memory calls (no TCP socket round-trips) |
| **Idle Memory RSS (Base)** | **291.9 MB** | Single Python process (Zero Redis / Zero Postgres) |
| **Active Memory RSS (Loaded ONNX)** | **420.0 MB** | Embedded vector model loaded in resident memory |
| **Runner & Benchmark Environment** | `Local System (Darwin arm64) | Python 3.12.16 | 100 reqs (c=1)` | Measured dynamically at `2026-11-15 17:21 UTC` |
<!-- BENCHMARK:END -->

### Speculative Latency Hedging (Simulated)

Production LLM endpoints frequently exhibit tail latency spikes from queue depth and GPU scheduling. SentinelGate implements speculative latency hedging: if the primary provider does not respond within `hedge_delay_ms=450`, a secondary request is dispatched to a fast fallback provider. Whichever completes first is returned.

<!-- HEDGING:START -->
| Execution Strategy | p50 Latency | p90 Latency | p95 Latency | p99 Latency (Tail) |
| :--- | :---: | :---: | :---: | :---: |
| Simulated Unhedged Requests | 401.6 ms | 621.7 ms | 2479.3 ms | **2518.9 ms** |
| **Speculative Hedged (`hedge_delay_ms=450.0`)** | **402.8 ms** | **428.2 ms** | **500.9 ms** | **506.3 ms** |
| **Improvement** | **+0.3%** | **-31.1%** | **-79.8%** | **-79.9% Latency Reduction** |

*Methodology: Simulated mock provider benchmark (`benchmarks/bench_hedging.py`) modeling bimodal upstream latency (10.0% probability of ~2480.0ms spike vs 400.0ms baseline, 450.0ms hedge deadline, 45.0ms fast fallback). Committed raw data: [`benchmarks/hedging_results.json`](benchmarks/hedging_results.json).*
<!-- HEDGING:END -->

Detailed benchmark scripts and reproduction steps are available in [`benchmarks/bench.py`](benchmarks/bench.py) and [`benchmarks/bench_hedging.py`](benchmarks/bench_hedging.py).

---

## Architectural Trade-Offs

SentinelGate is built as an embedded, single-process runtime. Full-featured gateways like [LiteLLM Proxy](https://docs.litellm.ai/docs/proxy/architecture) and [Portkey AI Gateway](https://docs.portkey.ai/docs/product/ai-gateway) offer broader multi-cloud provider ecosystems and enterprise control planes:

| Engineering Dimension | SentinelGate | LiteLLM Proxy ([Docs](https://docs.litellm.ai/docs/proxy/architecture)) | Portkey AI Gateway ([Docs](https://docs.portkey.ai/docs/product/ai-gateway)) |
| :--- | :--- | :--- | :--- |
| **Primary Architecture** | **Single-process embedded runtime** (FastAPI + SQLite WAL) | Standalone Python proxy or distributed multi-instance service | Open-source TypeScript proxy runtime or managed cloud SaaS |
| **External Dependencies** | **Zero** (In-process SQLite WAL + local ONNX runtime) | Zero for standalone CLI; Redis/Postgres optional for cluster state | Zero for standalone container; cloud DBs for control plane |
| **Provider Support** | Focused OpenAI-compatible backends (OpenAI, Anthropic, Groq, Ollama) | **100+ LLM providers** (Bedrock, Vertex AI, Azure, Cohere, etc.) | **250+ LLM providers** with unified multi-region routing |
| **Agent Tool & MCP Guardrail** | Built-in regex & AST tool-call inspector for destructive patterns | Pluggable third-party integrations (Presidio, Lakera, Aporia) | Configurable guardrails and enterprise policy rules |
| **Streaming Secret Redaction** | Stateful sliding window buffer ($N=64$ chars lookahead) | Middleware & callback hooks; Presidio PII plugins | Config-driven PII redaction and enterprise privacy rules |
| **Distributed Tracing** | Zero-dependency W3C `traceparent` parsing + raw OTLP JSON | OpenTelemetry SDK, Prometheus, Datadog, Langfuse | Built-in trace analytics, OpenTelemetry exporter |
| **State & Persistence** | Local SQLite database with Write-Ahead Logging (`WAL`) | Memory (single-node) or distributed Redis + PostgreSQL | Local SQLite/Redis or managed multi-region datastores |
| **Best Suited For** | **Single-host deployments, local coding agents, zero-infra edge proxies** | **Enterprise teams needing 100+ provider mappings and team billing** | **Production web apps needing hosted control planes and prompt governance** |

---

## Documentation

- **[System Architecture (RFC)](ARCHITECTURE.md):** In-process request lifecycle, SQLite WAL storage model, and concurrency design.
- **[Client & IDE Integrations](docs/INTEGRATIONS.md):** Setup instructions for Cursor, Cline, Claude Code CLI, Ollama, and SDKs.
- **[Configuration Reference](docs/CONFIGURATION.md):** Configuration schema, rate limits, virtual keys, and routing options.
- **[Security & Threat Model](docs/SECURITY.md):** Adversarial test coverage, bypass analysis, and defense boundaries.

---

## Roadmap

- **v0.5.0 (Current):** Single-process gateway, OpenAI `/v1/chat/completions` and Anthropic `/v1/messages` compatibility, in-process FastEmbed ONNX caching, heuristic tool guardrails, speculative latency hedging, local SQLite WAL state.
- **v0.6.0:** Tree-sitter / AST-based shell parsing for tool-call validation, pluggable vector index backends (e.g. LanceDB) for large cache indices.
- **v0.7.0:** Raft / LiteVFS replication for multi-instance SQLite WAL clustering without external database dependencies.
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
