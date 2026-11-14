# SentinelGate 🛡️

> **Zero-Bloat, OpenAI-Compatible LLM Gateway in Pure Python**  
> Direct provider REST streaming, sub-15ms local semantic caching, spend circuit breaker, bidirectional PII guardrails, adaptive auto-routing, 1-shot JSON self-repair, and asynchronous shadow evaluation. Core source code is strictly under **1,400 lines** with zero heavyweight orchestration frameworks.

[![CI](https://github.com/sentinelgate/sentinelgate/actions/workflows/ci.yml/badge.svg)](https://github.com/sentinelgate/sentinelgate/actions)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg)](https://fastapi.tiangolo.com/)
[![Built with uv](https://img.shields.io/badge/built%20with-uv-purple.svg)](https://github.com/astral-sh/uv)
[![Code Style: Ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

---

## Architecture Overview

```mermaid
flowchart TD
    Client["Client / Application\n(OpenAI SDK, cURL, LangChain)"] -->|"POST /v1/chat/completions"| SG["SentinelGate Proxy\n(FastAPI)"]
    
    subgraph Guardrails ["1. Pre-Processing & Guardrails"]
        SG --> Opt["Prompt Token Optimizer\n(Collapse redundant whitespace outside code)"]
        Opt --> PII["PII Masker\n(Emails, Phones, API Keys)"]
        PII --> Budget{"Spend Circuit Breaker\n(Daily Cap Check)"}
        Budget -->|"Breached"| Stop["HTTP 429: Budget Cap Exceeded"]
        Budget -->|"Allowed"| CacheCheck{"Cache Check\n(X-No-Cache?)"}
    end

    subgraph Caching ["2. Local Semantic Cache (ONNX + SQLite)"]
        CacheCheck -->|"Bypass: False"| FastEmbed["FastEmbed (BGE-Small ONNX)"]
        FastEmbed --> Cosine["NumPy Cosine Similarity\n(Threshold > 0.92)"]
        Cosine -->|"Hit (Cost: $0.00)"| CacheHit["Instant Cached Response\n(~12ms)"]
    end

    subgraph CoreProxy ["3. Smart Router & Failover"]
        Cosine -->|"Miss"| SmartRoute{"Model == 'auto'?"}
        SmartRoute -->|"Simple Query"| Tier1["Tier 1: Fast & Low-Cost\n(llama-3.1-8b-instant)"]
        SmartRoute -->|"Complex / Code"| Tier2["Tier 2: Flagship Reasoning\n(llama-3.3-70b / gpt-4o)"]
        SmartRoute -->|"Explicit Model"| Upstream["Async Client Pool\n(HTTP Keep-Alive)"]
        Tier1 --> Upstream
        Tier2 --> Upstream
        Upstream -->|"Round-Robin Key"| Providers{"Provider Call"}
        Providers -->|"Primary"| OpenAI["OpenAI REST API\n(gpt-4o, gpt-4o-mini)"]
        Providers -->|"Failover on 429/503"| Groq["Groq REST API\n(llama-3.3-70b)"]
        Providers -->|"Failover"| Gemini["Google Gemini REST\n(gemini-1.5-flash)"]
        Providers -->|"Local"| Ollama["Ollama Local\n(llama3, mistral)"]
    end

    subgraph PostProcessing ["4. Self-Healing & Restoration"]
        OpenAI --> JSONCheck{"JSON Object\nRequested?"}
        Groq --> JSONCheck
        Gemini --> JSONCheck
        Ollama --> JSONCheck
        JSONCheck -->|"Invalid Syntax"| SelfHeal["1-Shot Self-Repair Retry\n(Feed error back to LLM)"]
        JSONCheck -->|"Valid"| PIIRestore["Restore Masked PII"]
        SelfHeal --> PIIRestore
        CacheHit --> PIIRestore
    end

    subgraph Telemetry ["5. Telemetry & Playground"]
        PIIRestore -->|"Final Response"| Client
        PIIRestore -.->|"Log Metrics & Spend"| DB[("SQLite: sentinelgate.db")]
        PIIRestore -.->|"Sample 10% Uncached"| Shadow["Background Task:\nShadow Replay & Parity Judge"]
        Shadow -.->|"Quality Score (1-10)"| DB
        DB --> Dashboard["GET /dashboard\n(Dark-Mode UI + Live Playground)"]
    end
```

---

## Key Engineering Highlights

- **Zero Framework Bloat**: Completely avoids `langchain`, `litellm`, `openai`, `anthropic`, `torch`, or `sentence-transformers`. Talks directly to provider REST endpoints using pure `httpx.AsyncClient` with connection pooling.
- **Microsecond Semantic Caching**: Uses `fastembed` (quantized ONNX runtime) and vectorized NumPy cosine similarity against SQLite (`sentinelgate.db`). Queries with >0.92 cosine similarity resolve in ~13ms with **$0.00 API cost**. Supports `X-No-Cache: true` bypass header.
- **Common-Sense Spend Circuit Breaker**: Tracks cumulative daily USD spend across all models. If spending reaches your daily budget limit (e.g. `$10.00/day`), SentinelGate automatically halts upstream calls with HTTP `429`, protecting teams against runaway credit card bills and rogue loops.
- **Adaptive Smart Auto-Routing (`model: "auto"`)**: Intelligently classifies prompt complexity: simple greetings and basic lookups route to ultra-cheap, ultra-fast models (`llama-3.1-8b-instant`), while complex architectural and coding queries route to flagship models (`llama-3.3-70b-versatile` / `gpt-4o`).
- **Zero-Loss Prompt Token Optimizer**: Automatically eliminates redundant whitespace, trailing blanks, and multi-line gaps outside code blocks before dispatching upstream, cutting token costs by 5–15% without altering meaning or code indentation.
- **Resilient Key Rotation & 429/503 Failover**: Automatically rotates provider API keys round-robin. If a provider returns HTTP `429` (Rate Limited) or `503` (Overloaded), SentinelGate automatically retries alternative keys and seamlessly cascades down configured fallback models.
- **Reversible PII Masking**: Automatically redacts emails, phone numbers, and API tokens with cryptographic placeholders before prompts leave your network, and reconstructs original data into the final output.
- **1-Shot JSON Self-Healing**: When `response_format={"type": "json_object"}` is requested and an LLM outputs malformed JSON, SentinelGate intercepts the decode error and prompts the model with a 1-shot repair retry.
- **Continuous 10% Shadow Parity Evals**: Samples 10% of production traffic asynchronously via FastAPI `BackgroundTasks`, replays queries to cheaper alternative models, and scores parity (1–10) using an LLM-as-a-judge prompt.
- **Dark-Mode Observability & Live Playground**: Built-in, zero-dependency dashboard served at `/dashboard` featuring a real-time budget meter, P50/P95 latency, shadow eval win-rates, and an **interactive in-browser playground** to test prompts live.

---

## Benchmark Results

Benchmark executed with `uv run python benchmark.py` across **100 simulated requests** with mock upstream network latency (180ms–280ms) and semantic query clustering:

| Metric | Value |
|---|---|
| **Total Test Requests** | 100 |
| **Cache Hit Rate** | 91.0% (91/100) |
| **Average Uncached Latency (Upstream LLM)** | 361.95 ms |
| **Average Semantic Cache Latency** | 14.57 ms |
| **Semantic Cache Speedup** | **24.8x faster** |
| **Latency P50** | 13.25 ms |
| **Latency P90** | 34.39 ms |
| **Latency P95** | 259.77 ms |
| **Latency P99** | 705.93 ms |
| **Cost Incurred** | $0.00009 |
| **Cost Saved (via Cache)** | **$0.00091** |

---

## Quickstart

### 1. Prerequisites & Installation
Ensure `uv` is installed ([docs.astral.sh/uv](https://docs.astral.sh/uv/)):
```bash
# Clone the repository
git clone https://github.com/your-username/sentinelgate.git
cd sentinelgate

# Install dependencies using uv
uv sync
```

### 2. Configure Environment
Copy `.env.example` to `.env` and set your API keys:
```bash
cp .env.example .env
export OPENAI_API_KEY="sk-proj-..."
export GROQ_API_KEY="gsk_..."
export GEMINI_API_KEY="AIzaSy..."
export SENTINELGATE_DAILY_BUDGET=10.0
```

### 3. Start SentinelGate
```bash
uv run uvicorn main:app --host 0.0.0.0 --port 8000
```
- **Health Check**: `http://localhost:8000/health`
- **Models Catalog**: `http://localhost:8000/v1/models`
- **Chat Completions**: `http://localhost:8000/v1/chat/completions`
- **Live Observability & Playground**: `http://localhost:8000/dashboard`

---

## Usage Examples

### 1. Smart Adaptive Auto-Routing
```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [
      {"role": "user", "content": "Hello! Give me a quick joke."}
    ]
  }'
```

### 2. Standard Chat Completion
```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [
      {"role": "user", "content": "What is the capital of France?"}
    ]
  }'
```

### 3. Streaming SSE Response
```bash
curl -N -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o-mini",
    "stream": true,
    "messages": [
      {"role": "user", "content": "Count from 1 to 5 slowly."}
    ]
  }'
```

### 4. Bypassing Semantic Cache
```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-No-Cache: true" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "Give me current stock price."}]
  }'
```

---

## Testing & Quality Assurance

All tests run **100% offline** without live API keys using `pytest` and `respx` network virtualization:

```bash
# Run Ruff linting
uv run ruff check .

# Run complete offline test suite (13 tests)
uv run pytest -v

# Run the 100-request benchmark suite
uv run python benchmark.py
```

### Test Coverage Highlights
- ✅ `test_health_and_models`: Gateway diagnostics and model catalog discovery.
- ✅ `test_key_rotation`: Deterministic round-robin key scheduling per provider.
- ✅ `test_429_failover_to_backup_model`: Dynamic recovery from rate limit errors to fallback models.
- ✅ `test_503_failover`: Dynamic recovery from server overload/outage errors.
- ✅ `test_semantic_cache_hit_and_zero_cost`: Sub-15ms cache retrieval with zero upstream invocation and $0.00 cost.
- ✅ `test_cache_bypass_header`: Explicit cache evasion via `X-No-Cache: true`.
- ✅ `test_pii_masking_and_restoration`: Bidirectional redaction and restoration of emails, phone numbers, and API tokens.
- ✅ `test_json_self_healing_retry`: Automatic 1-shot self-repair on syntactically invalid JSON outputs.
- ✅ `test_streaming_sse_response`: Wire-compatible Server-Sent Events (SSE) streaming chunks.
- ✅ `test_budget_circuit_breaker`: Immediate short-circuiting when daily spending cap is breached.
- ✅ `test_adaptive_smart_auto_routing`: Automatic tiering between Tier 1 (fast/cheap) and Tier 2 (flagship) based on prompt heuristics.
- ✅ `test_prompt_optimizer_preserves_code_fences`: Whitespace trimming outside code blocks while keeping code intact.
- ✅ `test_dashboard_and_metrics_endpoints`: Telemetry aggregation and dark-mode UI delivery.

---

## Production Docker Deployment

Build and run using the optimized multi-stage `Dockerfile`:

```bash
# Build Docker image
docker build -t sentinelgate:latest .

# Run container
docker run -d -p 8000:8000 \
  --name sentinelgate \
  -e OPENAI_API_KEY="sk-proj-..." \
  -e GROQ_API_KEY="gsk_..." \
  -e SENTINELGATE_DAILY_BUDGET=10.0 \
  sentinelgate:latest

# Check health
curl http://localhost:8000/health
```

---

## Codebase Size & Transparency

| Module | Purpose | Lines of Code |
|---|---|---|
| `router.py` | Direct REST proxy, auto-tiering, key rotation & failover | ~340 |
| `cache.py` | FastEmbed ONNX & NumPy cosine semantic cache | ~185 |
| `guardrails.py` | Reversible PII masking, JSON self-healing, prompt optimizer | ~235 |
| `evals.py` | Telemetry logging & 10% shadow evaluation replay | ~265 |
| `budget.py` | Daily spend tracker & cost circuit breaker | ~75 |
| `main.py` | FastAPI application endpoints & lifecycle | ~285 |
| **Total Core Code** | **Complete Zero-Bloat LLM Gateway** | **~1,385 lines** |

---

## License
MIT License. Built for high-reliability, cost-conscious, security-minded AI engineering teams.
