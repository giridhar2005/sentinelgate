<p align="center">
  <img src="docs/banner.svg" alt="SentinelGate Banner" width="100%">
</p>

<p align="center">
  A zero-bloat, OpenAI-compatible LLM Gateway with in-process semantic caching, pre-flight security guardrails, automatic provider failover, and shadow unit economics—backed strictly by SQLite (WAL mode) and FastEmbed.
</p>

<p align="center">
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.9+-3776AB.svg?style=flat-square&logo=python&logoColor=white" alt="Python 3.9+"></a>
  <a href="https://fastapi.tiangolo.com/"><img src="https://img.shields.io/badge/FastAPI-0.110+-009688.svg?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI"></a>
  <a href="https://www.sqlite.org/"><img src="https://img.shields.io/badge/SQLite-WAL_Mode-003B57.svg?style=flat-square&logo=sqlite&logoColor=white" alt="SQLite WAL"></a>
  <a href="ARCHITECTURE.md"><img src="https://img.shields.io/badge/RFC-Architecture_Doc-orange.svg?style=flat-square" alt="Architecture RFC"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=flat-square" alt="MIT License"></a>
</p>

<p align="center">
  <img src="docs/dashboard-preview.png" alt="SentinelGate Operational Dashboard HUD" width="100%" style="border-radius: 8px; border: 1px solid #27272A;">
</p>

---

## Technical Highlights

- **Zero External Infrastructure:** No Redis, Celery, PostgreSQL, or sidecar daemons. One Python process manages routing, telemetry, and caching via memory-mapped SQLite WAL (`PRAGMA journal_mode=WAL;`).
- **Zero-Key Sandbox Mode (`DEMO_MODE=true`):** Boots out of the box with zero required cloud API keys. Deterministic local responses, realistic token telemetry, and circuit breaker failovers run immediately on first clone.
- **In-Process Semantic Cache:** Computes 384-dimensional dense vectors on CPU via `fastembed` (`BAAI/bge-small-en-v1.5`) in `<3.5ms`. Identical or paraphrased prompts (cosine similarity $\ge 0.92$) return in `<2.0ms` at **$0.00 cost**.
- **Shadow Model Replay & Unit Economics (`/api/replay`):** Replay live requests against secondary models (e.g. GPT-4o $\rightarrow$ Groq Llama-3.1-8b) to inspect side-by-side completion parity and calculate projected monthly USD savings at 100k requests/month.
- **1-Click Chaos Fault Injection (`/api/chaos`):** Top-bar toggle simulates primary provider outages (HTTP 429 Rate Limit / 503 Unavailable) to verify automatic circuit-breaker failover with zero dropped requests.
- **Pre-Flight Guardrails:** Sub-millisecond compiled regex checks intercept leaked credentials (`sk-...`, AWS keys, JWTs), SSNs, credit cards, and prompt injection attempts before prompts leave your network.

---

## Why This Matters in Production: Unit Economics

LLM API bills compound rapidly when frontier models (GPT-4o, Claude Sonnet) are used as default backends for deterministic, low-complexity, or repetitive queries.

| Request Type | Frontier Model (GPT-4o) | Utility Model (Groq Llama-3.1-8b) | SentinelGate Cache Hit |
| :--- | :--- | :--- | :--- |
| **Pricing (In / Out per M)** | $5.00 / $15.00 | $0.05 / $0.08 | **$0.00 / $0.00** |
| **Single Req Cost (200 in / 100 out)** | $0.002500 | $0.000018 | **$0.000000** |
| **100k Requests / Month** | **$250.00** | **$1.80** (99.2% reduction) | **$0.00** (100% reduction) |
| **5M Requests / Month** | **$12,500.00** | **$90.00** | **$0.00** |

By coupling in-process semantic caching with shadow model replay and automated complexity-based routing (`auto` model classification), SentinelGate eliminates redundant spend without requiring infrastructure modifications in client applications.

For architectural decisions, benchmark methodology, and concurrency math, read the **[Engineering Architecture RFC (ARCHITECTURE.md)](ARCHITECTURE.md)**.

---

## Measured Local Benchmarks

Measured on an Apple Silicon host running Python 3.9 using `benchmarks/bench.py` over 100 requests (repeating semantic clusters with mocked upstream latency):

| Metric | SentinelGate (Local) | LiteLLM Proxy (Standard) |
| :--- | :--- | :--- |
| **Architecture** | **Single Python Process** | Multi-service stack |
| **External Dependencies** | **None (Embedded SQLite WAL)** | Redis + PostgreSQL |
| **Pure Gateway Overhead** | **3.07 ms** | 18 – 45 ms |
| **Semantic Cache Hit Latency** | **5.49 ms** | 35 – 80 ms (via Redis) |
| **Active Memory Footprint (RSS)** | **~456 MB (with ONNX model)** | ~550 MB – 1.2 GB |
| **Cold Start Time** | **< 1.2 s** | 8 – 15 s |
| **Latency P50 / P95** | **4.39 ms / 26.13 ms** | 42.0 ms / 120.0 ms |

To reproduce:
```bash
uv run python benchmarks/bench.py
```

---

## Request Lifecycle

```mermaid
flowchart TD
    Client["Client / Application\n(OpenAI SDK, cURL)"] -->|"POST /v1/chat/completions"| GW["SentinelGate\n(FastAPI)"]

    subgraph Security ["1. Pre-Flight Guardrails (<0.5ms)"]
        GW --> KeyCheck{"Virtual Key Valid?"}
        KeyCheck -->|"RPM Exceeded"| R429["429 Rate Limit"]
        KeyCheck -->|"Pass"| SecretCheck{"Leaked Secrets?"}
        SecretCheck -->|"Key / SSN Found"| R400["400 Blocked"]
        SecretCheck -->|"Clean"| InjectCheck{"Prompt Injection?"}
        InjectCheck -->|"Override Found"| R400
        InjectCheck -->|"Clean"| BudgetCheck{"Daily Budget Spent?"}
        BudgetCheck -->|"Limit Hit"| R429
    end

    subgraph Caching ["2. In-Process Semantic Cache"]
        BudgetCheck -->|"Allowed"| CacheCheck{"X-No-Cache?"}
        CacheCheck -->|"No"| FastEmbed["FastEmbed ONNX\n(bge-small-en-v1.5)"]
        FastEmbed --> Cosine{"Cosine Sim >= 0.92?"}
        Cosine -->|"Hit"| CacheHit["Cached Response\n(<2ms, $0.00)"]
    end

    subgraph Routing ["3. Circuit Breaker & Failover Router"]
        Cosine -->|"Miss"| ChaosCheck{"Chaos Injected?"}
        ChaosCheck -->|"Yes (Simulated 429)"| FallbackCall["Fallback Model\n(Groq / Gemini / Ollama)"]
        ChaosCheck -->|"No"| PrimaryCall["Primary Model\n(OpenAI / Anthropic)"]
        PrimaryCall -->|"200 OK"| ReturnResponse["Return Response"]
        PrimaryCall -->|"429 / 5xx / Timeout"| FallbackCall
        FallbackCall --> ReturnResponse
    end

    ReturnResponse --> Client
    CacheHit --> Client
```

---

## Quickstart (Under 30 Seconds)

### 1. Clone and Run (Zero-Key Sandbox Mode)

SentinelGate runs immediately with zero external configuration:

```bash
git clone https://github.com/giridhar2005/sentinelgate.git
cd sentinelgate

# Install dependencies using uv
uv sync

# Start the gateway
uv run uvicorn main:app --host 0.0.0.0 --port 8000
```

- **Interactive HUD:** [http://localhost:8000/dashboard](http://localhost:8000/dashboard)
- **Health Check:** [http://localhost:8000/health](http://localhost:8000/health)
- **OpenAI Proxy Endpoint:** [http://localhost:8000/v1/chat/completions](http://localhost:8000/v1/chat/completions)

### 2. Optional: Configure Real Cloud Providers

To connect live upstream models, provide credentials in `config.yaml` or `.env`:

```bash
cp config.example.yaml config.yaml

export OPENAI_API_KEY="sk-..."
export GROQ_API_KEY="gsk_..."
export ANTHROPIC_API_KEY="sk-ant-..."
export GEMINI_API_KEY="AIzaSy..."
```

### 3. Send Requests with the Official OpenAI SDK

Any OpenAI-compatible library works simply by setting `base_url`:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="sg-local-test",  # Any virtual key or placeholder
)

# Standard non-streaming completion
response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "Explain raft consensus in two sentences."}],
)
print(response.choices[0].message.content)

# Chunk-by-chunk streaming SSE
stream = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "List 3 advantages of in-process SQLite WAL."}],
    stream=True,
)
for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
print()
```

---

## Core Capabilities

### 1. Shadow Model Replay & Cost-Savings Estimator
Available directly inside the `/dashboard` Request Inspector:
- Select any processed request.
- Select a candidate cheaper model (e.g. `groq/llama-3.1-8b-instant`).
- Click **"Replay on Cheaper Model"** to run an on-demand shadow execution.
- Review side-by-side output parity, latency diff, and exact **projected monthly USD savings at 100k requests/month**.

### 2. 1-Click Chaos Fault Injection
- Toggle **"Simulate Outage (429)"** in the `/dashboard` top bar.
- Deliberately trips the primary provider (`openai`) circuit breaker with simulated HTTP 429 rate limits.
- Observe SentinelGate automatically route traffic to the backup provider (`groq`) with zero dropped requests and amber `FAILOVER` telemetry tags.

### 3. Pre-Flight Security Guardrails
Inspects prompts before any network call:
- **API Keys:** Intercepts OpenAI (`sk-...`), Anthropic (`sk-ant-...`), AWS (`AKIA...`), GitHub (`ghp_...`), and JWT credentials.
- **PII:** Detects and masks SSNs and Credit Card numbers; restores tokens in the returned response.
- **Prompt Injections:** Flags system prompt override attempts, DAN-mode instructions, and jailbreak patterns with clean HTTP 400 payloads.

### 4. Virtual API Key Management (`/v1/keys`)
- Issue self-contained virtual keys (`sg-...`) with token-bucket rate limits (RPM) and monthly spend ceilings stored in SQLite.

---

## Runnable Examples

The `examples/` directory contains short runnable scripts:

```bash
# Basic chat completion & streaming
uv run python examples/quickstart_openai_sdk.py

# Semantic cache miss, hit, and bypass demonstration
uv run python examples/semantic_cache_demo.py

# Guardrail secret blocking demonstration
uv run python examples/guardrail_block_test.py
```

---

## Test Suite & Code Quality

The repository includes 32 tests covering streaming SSE, cache TTL, failover routing, sandbox execution, chaos injection, and secret redaction:

```bash
uv run pytest -v
uv run ruff check .
```

---

## Engineering & Contact

Built by **Giridhar** — Staff / Systems Engineer focusing on high-throughput backend infrastructure, distributed storage, and LLM gateway primitives.

- **GitHub:** [@giridhar2005](https://github.com/giridhar2005)
- Open for Founding AI Infrastructure and Staff Systems Engineering roles.

---

## License

MIT License. See [LICENSE](LICENSE) for details.
