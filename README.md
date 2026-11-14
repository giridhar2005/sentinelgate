# SentinelGate 🛡️

> **Zero-Bloat, OpenAI-Compatible LLM Gateway in Pure Python**  
> Direct provider REST streaming, sub-15ms local semantic caching, bidirectional PII guardrails, 1-shot JSON self-repair, and asynchronous shadow evaluation. Core source code is strictly under **1,200 lines** with zero heavyweight orchestration frameworks.

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
        SG --> PII["PII Masker\n(Emails, Phones, API Keys)"]
        PII --> CacheCheck{"Cache Check\n(X-No-Cache?)"}
    end

    subgraph Caching ["2. Local Semantic Cache (ONNX + SQLite)"]
        CacheCheck -->|"Bypass: False"| FastEmbed["FastEmbed (BGE-Small ONNX)"]
        FastEmbed --> Cosine["NumPy Cosine Similarity\n(Threshold > 0.92)"]
        Cosine -->|"Hit (Cost: $0.00)"| CacheHit["Instant Cached Response\n(~12ms)"]
    end

    subgraph CoreProxy ["3. Direct REST Router & Failover"]
        Cosine -->|"Miss"| Upstream["Async Client Pool\n(HTTP Keep-Alive)"]
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

    subgraph Telemetry ["5. Telemetry & Shadow Evals"]
        PIIRestore -->|"Final Response"| Client
        PIIRestore -.->|"Log Metrics"| DB[("SQLite: sentinelgate.db")]
        PIIRestore -.->|"Sample 10% Uncached"| Shadow["Background Task:\nShadow Replay & Parity Judge"]
        Shadow -.->|"Quality Score (1-10)"| DB
        DB --> Dashboard["GET /dashboard\n(Dark-Mode Live UI)"]
    end
```

---

## Key Engineering Highlights

- **Zero Framework Bloat**: Completely avoids `langchain`, `litellm`, `openai`, `anthropic`, `torch`, or `sentence-transformers`. Talks directly to provider REST endpoints using pure `httpx.AsyncClient` with connection pooling.
- **Microsecond Semantic Caching**: Uses `fastembed` (quantized ONNX runtime) and vectorized NumPy cosine similarity against SQLite (`sentinelgate.db`). Queries with >0.92 cosine similarity resolve in ~12ms with **$0.00 API cost**. Supports `X-No-Cache: true` bypass header.
- **Resilient Key Rotation & 429/503 Failover**: Automatically rotates provider API keys round-robin. If a provider returns HTTP `429` (Rate Limited) or `503` (Overloaded), SentinelGate automatically retries alternative keys and seamlessly cascades down configured fallback models.
- **Reversible PII Masking**: Automatically redacts emails, phone numbers, and API tokens with cryptographic placeholders before prompts leave your network, and reconstructs original data into the final output.
- **1-Shot JSON Self-Healing**: When `response_format={"type": "json_object"}` is requested and an LLM outputs malformed JSON, SentinelGate intercepts the decode error and prompts the model with a 1-shot repair retry.
- **Continuous 10% Shadow Parity Evals**: Samples 10% of production traffic asynchronously via FastAPI `BackgroundTasks`, replays queries to cheaper alternative models, and scores parity (1–10) using an LLM-as-a-judge prompt.
- **Dark-Mode Live Observability**: Built-in, zero-dependency dashboard served at `/dashboard` tracking P50/P95 latency, cache hit ratios, cumulative USD savings, and shadow evaluation win-rates.

---

## Benchmark Results

Benchmark executed with `uv run python benchmark.py` across **100 simulated requests** with mock upstream network latency (180ms–280ms) and semantic query clustering:

| Metric | Value |
|---|---|
| **Total Test Requests** | 100 |
| **Cache Hit Rate** | 92.0% (92/100) |
| **Average Uncached Latency (Upstream LLM)** | 390.95 ms |
| **Average Semantic Cache Latency** | 12.58 ms |
| **Semantic Cache Speedup** | **31.1x faster** |
| **Latency P50** | 11.87 ms |
| **Latency P90** | 18.73 ms |
| **Latency P95** | 277.44 ms |
| **Latency P99** | 693.83 ms |
| **Cost Incurred** | $0.00008 |
| **Cost Saved (via Cache)** | **$0.00092** |

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
```

### 3. Start SentinelGate
```bash
uv run uvicorn main:app --host 0.0.0.0 --port 8000
```
- **Health Check**: `http://localhost:8000/health`
- **Models List**: `http://localhost:8000/v1/models`
- **Chat Completions**: `http://localhost:8000/v1/chat/completions`
- **Live Observability Dashboard**: `http://localhost:8000/dashboard`

---

## Usage Examples

### Using `cURL` (Standard Chat Completion)
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

### Streaming SSE Response
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

### Bypassing Cache Explicitly
```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-No-Cache: true" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "Give me current stock price."}]
  }'
```

### Using OpenAI Python Client
Because SentinelGate is 100% wire-compatible with the OpenAI specification, simply redirect `base_url`:
```python
# In your existing client application
import httpx

client = httpx.Client(base_url="http://localhost:8000/v1")
response = client.post("/chat/completions", json={
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "Explain quantum computing in one sentence."}]
})
print(response.json()["choices"][0]["message"]["content"])
```

---

## Testing & Quality Assurance

All tests run **100% offline** without live API keys using `pytest` and `respx` network virtualization:

```bash
# Run Ruff linting
uv run ruff check .

# Run complete offline test suite
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
  sentinelgate:latest

# Check health
curl http://localhost:8000/health
```

---

## Codebase Size & Transparency

| Module | Purpose | Lines of Code |
|---|---|---|
| `router.py` | Direct REST integration, key rotation & failover | ~280 |
| `cache.py` | FastEmbed ONNX & NumPy cosine semantic cache | ~180 |
| `guardrails.py` | Reversible PII masking & 1-shot JSON self-healing | ~190 |
| `evals.py` | Telemetry logging & 10% shadow evaluation replay | ~260 |
| `main.py` | FastAPI application endpoints & lifecycle | ~250 |
| **Total Core Code** | **Complete Zero-Bloat LLM Gateway** | **~1,160 lines** |

---

## License
MIT License. Built for high-reliability, security-conscious AI engineering teams.
