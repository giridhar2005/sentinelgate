<p align="center">
  <img src="docs/banner.svg" alt="SentinelGate Banner" width="100%">
</p>

<p align="center">
  <strong>One local gateway for all your AI tools. Cut API bills up to 70% with semantic caching and smart auto-routing (<code>model="auto"</code>), and scrub secrets before prompts leave your machine.</strong>
</p>

<p align="center">
  A zero-bloat, OpenAI-compatible LLM Gateway with in-process semantic caching, automatic provider failover, reversible secret redaction, and sub-millisecond cost routing—backed strictly by SQLite (WAL mode) and FastEmbed. Zero Redis. Zero PostgreSQL. Zero node_modules.
</p>

<p align="center">
  <a href="https://github.com/giridhar2005/sentinelgate/actions/workflows/ci.yml"><img src="https://github.com/giridhar2005/sentinelgate/actions/workflows/ci.yml/badge.svg" alt="CI Status"></a>
  <a href="https://github.com/giridhar2005/sentinelgate/releases"><img src="https://img.shields.io/github/v/release/giridhar2005/sentinelgate?style=flat-square&color=emerald" alt="Release"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.9%20%7C%203.10%20%7C%203.11%20%7C%203.12-3776AB.svg?style=flat-square&logo=python&logoColor=white" alt="Python 3.9+"></a>
  <a href="https://fastapi.tiangolo.com/"><img src="https://img.shields.io/badge/FastAPI-0.110+-009688.svg?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI"></a>
  <a href="https://www.sqlite.org/"><img src="https://img.shields.io/badge/SQLite-WAL_Mode-003B57.svg?style=flat-square&logo=sqlite&logoColor=white" alt="SQLite WAL"></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json" alt="Ruff"></a>
  <a href="ARCHITECTURE.md"><img src="https://img.shields.io/badge/RFC-Architecture_Doc-orange.svg?style=flat-square" alt="Architecture RFC"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=flat-square" alt="MIT License"></a>
</p>

<p align="center">
  <a href="#who-is-this-for">Audience</a> &bull;
  <a href="#measured-local-benchmarks">Benchmarks</a> &bull;
  <a href="#request-lifecycle">Lifecycle</a> &bull;
  <a href="#10-second-quickstart">Quickstart</a> &bull;
  <a href="#production-capabilities--architecture-deep-dives">Deep Dives</a> &bull;
  <a href="ARCHITECTURE.md">Systems RFC</a> &bull;
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

<p align="center">
  <img src="docs/dashboard-preview.png" alt="SentinelGate Operational Dashboard HUD" width="100%" style="border-radius: 8px; border: 1px solid #27272A;">
</p>

---

## Who Is This For?

- **Cursor, Cline & VS Code Users:** Drop SentinelGate in front of your editor. Stop burning API credits on repetitive prompt completions and prevent accidental leaks of local `.env` keys.
- **Indie Hackers & Startups:** Slash cloud LLM costs without running Redis or PostgreSQL clusters. One lightweight background process routes queries dynamically and saves hundreds of dollars every month.
- **Local + Cloud Hybrid Developers:** Combine local Ollama / LM Studio models with cloud frontier APIs (Claude Sonnet, GPT-4o). Let `model="auto"` handle simple queries locally at $0.00 cost while preserving frontier intelligence for complex reasoning.
- **Privacy-Conscious Teams:** Intercept and scrub credentials, database URIs, API keys, and sensitive PII with reversible redaction tokens before any prompt ever hits external networks.

---

## Measured Local Benchmarks

Measured on an Apple Silicon host running Python 3.9 using `benchmarks/bench.py` over 100 requests (repeating semantic clusters with mocked upstream latency):

| Metric | SentinelGate (Local) | LiteLLM Proxy (Standard) |
| :--- | :--- | :--- |
| **Architecture** | **Single Python Process** | Multi-service stack |
| **External Dependencies** | **None (Embedded SQLite WAL)** | Redis + PostgreSQL |
| **Pure Gateway Overhead (Min)** | **3.32 ms** | 18 – 45 ms |
| **Semantic Cache Hit Latency (Avg)**| **6.13 ms** | 35 – 80 ms (via Redis) |
| **Cache Hit Rate** | **91.0%** (91/100) | Varies |
| **Active Memory Footprint (RSS)** | **~466 MB (with ONNX model)** | ~550 MB – 1.2 GB |
| **Cold Start Time** | **< 1.2 s** | 8 – 15 s |
| **Latency P50 / P95** | **4.42 ms / 42.77 ms** | 42.0 ms / 120.0 ms |

To reproduce locally:
```bash
uv run python benchmarks/bench.py
```

---

## Request Lifecycle

```mermaid
flowchart TD
    Client["Client / Application\n(OpenAI SDK, cURL, Cursor)"] -->|"POST /v1/chat/completions"| GW["SentinelGate\n(FastAPI)"]

    subgraph Security ["1. Pre-Flight Guardrails (<0.3ms)"]
        GW --> KeyCheck{"Virtual Key Valid?"}
        KeyCheck -->|"RPM Exceeded"| R429["429 Rate Limit"]
        KeyCheck -->|"Pass"| SecretCheck{"Leaked Secrets?"}
        SecretCheck -->|"Block Mode"| R400["400 Blocked"]
        SecretCheck -->|"Redact Mode"| Scrub["Reversible Token\n(__SG_SECRET_1__)"]
        Scrub --> InjectCheck{"Prompt Injection?"}
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
        Cosine -->|"Miss"| AutoRoute{"model == 'auto'?"}
        AutoRoute -->|"Yes"| Classifier["Complexity Scorer (<0.2ms)\nSimple -> Utility | Complex -> Frontier"]
        AutoRoute -->|"No"| ProviderCall{"Chaos Injected?"}
        Classifier --> ProviderCall
        ProviderCall -->|"Simulate 429"| FallbackCall["Fallback Model\n(Groq / Gemini / Ollama)"]
        ProviderCall -->|"Normal"| PrimaryCall["Primary Model\n(OpenAI / Anthropic)"]
        PrimaryCall -->|"200 OK"| RestoreSecrets["Restore Secrets\n(__SG_SECRET__ -> Original)"]
        PrimaryCall -->|"429 / 5xx"| FallbackCall
        FallbackCall --> RestoreSecrets
    end

    RestoreSecrets --> Client
    CacheHit --> Client
```

---

## 10-Second Quickstart

### 1. Install & Launch

```bash
git clone https://github.com/giridhar2005/sentinelgate.git
cd sentinelgate

# Install as a standard Python tool
pip install -e .

# Launch the gateway (auto-discovers keys & local Ollama/LM Studio runtimes)
sentinelgate
```

Or run directly without installing:
```bash
python -m sentinelgate
# or with uv
uv run python -m sentinelgate
```

SentinelGate will print an auto-discovery summary table and start listening:
- **Proxy Endpoint:** `http://localhost:8000/v1`
- **Dashboard HUD:** `http://localhost:8000/dashboard`
- **Health Check:** `http://localhost:8000/health`

> [!NOTE]
> **Zero-Key Sandbox Mode:** If no cloud provider API keys (`OPENAI_API_KEY`, etc.) or local model runtimes are detected, SentinelGate boots automatically into deterministic sandbox mode. The interactive HUD and `/v1/chat/completions` endpoint work out of the box on first clone.

### 2. Connect Your Tools in 5 Seconds

Click the **"Connect Tool"** button on the `/dashboard` HUD for instant configuration snippets, or use standard setups:

#### Cursor / Cline / VS Code
Set OpenAI Base URL to:
```
http://localhost:8000/v1
```
Set Model Name to `auto` (or `gpt-4o-mini`, `claude-3-5-sonnet-20241022`).

> [!TIP]
> In Cursor or Cline, set `Model: auto` to let SentinelGate automatically route code diffs and complex architecture questions to frontier models while routing routine formatting and haikus to cheap utility backends.

#### Terminal / CLI Environment
```bash
export OPENAI_BASE_URL="http://localhost:8000/v1"
export OPENAI_API_KEY="sk-sentinelgate-local"

# Any CLI tool (aider, llm, fabric, curl) now routes through SentinelGate
curl $OPENAI_BASE_URL/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "auto", "messages": [{"role": "user", "content": "Write a haiku about trees."}]}'
```

#### Official Python SDK
```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="sk-sentinelgate-local"
)

# Pass model="auto" for automatic cost-optimized routing
response = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Explain raft consensus in 2 sentences."}]
)
print(response.choices[0].message.content)
```

---

## Production Capabilities & Architecture Deep Dives

<details>
<summary><b>1. Universal Smart Cost-Router (<code>model="auto"</code>)</b></summary>

Frontier models (Claude 3.5 Sonnet, GPT-4o) cost upwards of $5.00 / $15.00 per million tokens. Passing `model="auto"` activates a deterministic, sub-millisecond prompt complexity classifier:

- **Simple queries** (haikus, general Q&A, translation, routine formatting) route to local Ollama ($0.00), Groq Llama-3.1, or GPT-4o-mini—saving up to 99% vs. frontier pricing.
- **Complex queries** (system architecture, distributed concurrency, debugging, multi-turn reasoning) route to frontier backends (Claude 3.5 Sonnet, GPT-4o).
- Every auto-routed response injects telemetry headers:
  - `X-Auto-Route: ollama/llama3`
  - `X-Auto-Saved-USD: 0.001285`
  - `X-Auto-Complexity: simple (10/100)`
</details>

<details>
<summary><b>2. Reversible Secret & PII Shield (<code>redact</code> + <code>block</code> modes)</b></summary>

Inspects incoming prompts in sub-millisecond compiled regex before any network call:
- **API Keys:** Scans for OpenAI (`sk-...`), Anthropic (`sk-ant-...`), AWS access keys (`AKIA...`), GitHub personal access tokens (`ghp_...`), and JWT credentials.
- **Database URIs:** Intercepts `postgresql://`, `mysql://`, `redis://`, and `mongodb://` connection strings.
- **Dual-Mode Operation:**
  - `redact` (default): Replaces detected secrets with reversible placeholders (`__SG_SECRET_1__`) outbound and restores the original values in the response stream inbound—protecting developers without breaking their workflow.
  - `block`: Returns strict HTTP 400 with security violation error payload.
</details>

<details>
<summary><b>3. In-Process Semantic Cache & fastembed ONNX</b></summary>

- Generates 384-dimensional dense vectors on CPU in `<3.5ms` using `fastembed` (`BAAI/bge-small-en-v1.5`).
- Queries SQLite Write-Ahead Log (`sentinelgate.db`) with memory-mapped I/O (`PRAGMA mmap_size = 268435456;`).
- When cosine similarity $\ge 0.92$, returns cached responses in `<2ms` at **$0.00 cost**.
- Respects `Cache-Control: no-cache` and `X-No-Cache: true` bypass headers.
</details>

<details>
<summary><b>4. Shadow Model Replay & Unit Economics (<code>/api/replay</code>)</b></summary>

Available directly inside the `/dashboard` Request Inspector:
- Select any processed request.
- Select a candidate cheaper model (e.g. `groq/llama-3.1-8b-instant`).
- Click **"Replay on Cheaper Model"** to run an on-demand shadow execution.
- Review side-by-side output parity, latency diff, and exact **projected monthly USD savings at 100k requests/month**.
</details>

<details>
<summary><b>5. 1-Click Chaos Outage Injection (<code>/api/chaos</code>)</b></summary>

- Toggle **"Simulate Outage (429)"** in the `/dashboard` top bar.
- Deliberately trips the primary provider (`openai`) circuit breaker with simulated HTTP 429 rate limits.
- Observe SentinelGate automatically route traffic to the backup provider (`groq`) with zero dropped requests and amber `FAILOVER` telemetry tags.
</details>

<details>
<summary><b>6. Virtual API Key Management (<code>/v1/keys</code>)</b></summary>

- Issue self-contained virtual keys (`sg-...`) with token-bucket rate limits (RPM) and monthly spend ceilings stored in SQLite.
- Fully isolated tenant accounting without requiring an external authentication service.
</details>

<details>
<summary><b>7. Full YAML Configuration Reference (<code>config.yaml</code>)</b></summary>

```yaml
server:
  host: "0.0.0.0"
  port: 8000

cache:
  enabled: true
  similarity_threshold: 0.92
  ttl_seconds: 3600
  db_path: "sentinelgate.db"

guardrails:
  secret_action: "redact"  # "redact" (reversible shield) or "block" (strict HTTP 400)
  pii_masking: true
  block_secrets: false
  prompt_injection_block: true
  max_repair_attempts: 1

budget:
  enabled: true
  daily_limit_usd: 10.0

providers:
  openai:
    type: "openai"
    base_url: "https://api.openai.com/v1"
    api_keys: ["${OPENAI_API_KEY}"]
    models:
      gpt-4o:
        input_cost_per_m: 5.0
        output_cost_per_m: 15.0
        fallbacks: ["groq/llama-3.3-70b-versatile", "anthropic/claude-3-5-sonnet-20241022"]
      gpt-4o-mini:
        input_cost_per_m: 0.15
        output_cost_per_m: 0.60
        fallbacks: ["groq/llama-3.1-8b-instant", "ollama/llama3"]
```
</details>

---

## Test Suite & Code Quality

The repository includes 36 tests covering streaming SSE, semantic cache TTL, automatic failover routing, sandbox execution, chaos injection, reversible secret redaction, and `model="auto"` complexity scoring:

```bash
uv run pytest -v
uv run ruff check .
```

---

## Engineering & Contact

Built by **Giridhar** — Systems Engineer focusing on high-throughput backend infrastructure, distributed storage, and LLM gateway primitives.

Open for Founding AI Infrastructure, Staff Systems, and Developer Tools Engineering roles. Reach out via [GitHub](https://github.com/giridhar2005) or open an issue.

---

## Star History

<p align="center">
  <a href="https://star-history.com/#giridhar2005/sentinelgate&Date">
    <img src="https://api.star-history.com/svg?repos=giridhar2005/sentinelgate&type=Date&theme=dark" alt="SentinelGate Star History" width="760">
  </a>
</p>

---

## License

MIT License. See [LICENSE](LICENSE) for details.
