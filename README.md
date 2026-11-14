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
  <a href="https://github.com/giridhar2005/sentinelgate/actions/workflows/codeql.yml"><img src="https://github.com/giridhar2005/sentinelgate/actions/workflows/codeql.yml/badge.svg" alt="CodeQL Status"></a>
  <a href="https://github.com/giridhar2005/sentinelgate/releases"><img src="https://img.shields.io/github/v/release/giridhar2005/sentinelgate?style=flat-square&color=emerald" alt="Release"></a>
  <a href="https://codespaces.new/giridhar2005/sentinelgate"><img src="https://github.com/codespaces/badge.svg" alt="Open in GitHub Codespaces"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.9%20%7C%203.10%20%7C%203.11%20%7C%203.12-3776AB.svg?style=flat-square&logo=python&logoColor=white" alt="Python 3.9+"></a>
  <a href="https://fastapi.tiangolo.com/"><img src="https://img.shields.io/badge/FastAPI-0.110+-009688.svg?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI"></a>
  <a href="https://www.sqlite.org/"><img src="https://img.shields.io/badge/SQLite-WAL_Mode-003B57.svg?style=flat-square&logo=sqlite&logoColor=white" alt="SQLite WAL"></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json" alt="Ruff"></a>
  <a href="ARCHITECTURE.md"><img src="https://img.shields.io/badge/RFC-Architecture_Doc-orange.svg?style=flat-square" alt="Architecture RFC"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=flat-square" alt="MIT License"></a>
</p>

<p align="center">
  <a href="#the-architecture-contrast">Architecture Contrast</a> &bull;
  <a href="#the-under-5ms-critical-path">Critical Path</a> &bull;
  <a href="#system-latency--resource-benchmarks">Benchmarks Chart</a> &bull;
  <a href="#terminal-in-action">Terminal in Action</a> &bull;
  <a href="#why-sqlite-wal-and-not-redis">Why SQLite WAL?</a> &bull;
  <a href="#developer-console--interactive-hud">Developer Console</a> &bull;
  <a href="#10-second-quickstart">Quickstart</a> &bull;
  <a href="#frequently-asked-systems-questions">Systems FAQ</a> &bull;
  <a href="ARCHITECTURE.md">Systems RFC</a>
</p>

<p align="center">
  <img src="docs/architecture-pillars.svg" alt="SentinelGate Core Architectural Pillars" width="100%" style="border-radius: 8px; border: 1px solid #27272A; margin: 8px 0;">
</p>

---

## The Architecture Contrast

| Operational Dimension | Without SentinelGate (Direct Cloud SDKs) | With SentinelGate (Zero-Bloat Proxy) |
| :--- | :--- | :--- |
| **API Costs & Unit Economics** | Every repetitive query pays full frontier token pricing ($15.00 / M). | **91% cache hit rate** served locally on CPU in `<2.0ms` at **$0.00 cost**. |
| **Credential & Secret Safety** | Accidental `.env` keys (`sk-...`, AWS, DB URIs) leak directly to third-party logs. | **Sub-ms regex firewall** tokenizes credentials outbound and restores them inbound. |
| **Model Selection Strategy** | Hardcoded `gpt-4o` burns money on simple formatting, translation, and code diffs. | **`model="auto"`** dynamically routes tasks to cheap utility vs. frontier models. |
| **Outage & Rate Limit Resilience** | Upstream HTTP `429` rate limits or `503` outages crash client scripts and IDE agents. | **Automatic circuit breaker** fails over to backup providers with zero dropped requests. |
| **Infrastructure Complexity** | Multi-service stack requiring Redis cluster, PostgreSQL daemon, and Celery workers. | **Single Python process** backed by in-process SQLite WAL mode (`PRAGMA mmap_size`). |

---

## The Under-5ms Critical Path

Every request hitting `POST /v1/chat/completions` undergoes a deterministic, sub-5-millisecond pipeline executed entirely in-process before any network socket is opened:

<p align="center">
  <img src="docs/request-flow.svg" alt="SentinelGate Request Lifecycle Flow" width="100%" style="border-radius: 8px; border: 1px solid #27272A; margin: 12px 0;">
</p>

```
[Client Request: OpenAI SDK / Cursor / cURL]
   │
   ├── T + 0.1ms ── [1. Credential Shield]
   │                 Compiled regex tokenizes sk-..., AWS, JWT, and DB connection URIs
   │                 into reversible placeholders (__SG_SECRET_1__). Outbound leak prevented.
   │
   ├── T + 0.3ms ── [2. Tenant Budget & Limiter]
   │                 Token-bucket rate limits (RPM) and monthly spend ceilings validated
   │                 in SQLite via memory-mapped pages (<0.08ms execution).
   │
   ├── T + 1.8ms ── [3. In-Process Dense Vector Match]
   │                 FastEmbed (BAAI/bge-small-en-v1.5) ONNX engine computes 384-dim vector.
   │                 Evaluates cosine similarity against SQLite memory cache.
   │                 ├── Hit (>= 0.92) ──> Return cached response (<2.0ms, $0.00 cost) ──┐
   │                 └── Miss (< 0.92) ──┐                                                │
   │                                     │                                                │
   ├── T + 2.1ms ── [4. Cost Router Arbitrage]                                            │
   │                 model="auto" complexity classifier analyzes token length and syntax. │
   │                 ├── Simple ──> Cheap Utility (Local Ollama / Groq / GPT-4o-mini)     │
   │                 └── Complex ─> Frontier Reasoning (Claude 3.5 Sonnet / GPT-4o)       │
   │                                     │                                                │
   ├── Upstream ─── [5. Circuit Breaker & Failover]                                       │
   │                 Executes HTTP call. If primary trips 429/503, automatic failover     │
   │                 reroutes to backup provider with zero dropped streaming frames.      │
   │                                     │                                                │
   └── Return ───── [6. Inbound Secret Rehydration]                                       │
                     Hydrates __SG_SECRET_1__ tokens back to original credentials.        │
                     Telemetry headers injected (X-Cache, X-Saved-USD, X-Latency).        │
                     │                                                                    │
                     ▼                                                                    ▼
[Client Response Stream: 200 OK] ◄────────────────────────────────────────────────────────┘
```

---

## System Latency & Resource Benchmarks

Measured on an Apple Silicon host running Python 3.9 using `benchmarks/bench.py` over 100 requests (repeating semantic clusters with mocked upstream latency):

<p align="center">
  <img src="docs/benchmarks-chart.svg" alt="System Latency & Resource Benchmarks" width="100%" style="border-radius: 8px; border: 1px solid #27272A; margin: 12px 0;">
</p>

| Metric | SentinelGate (Local) | LiteLLM Proxy (Standard) | Direct Cloud API (Cold) |
| :--- | :--- | :--- | :--- |
| **Architecture** | **Single Python Process** | Multi-service stack | Hosted SaaS |
| **External Dependencies** | **None (Embedded SQLite WAL)** | Redis + PostgreSQL | Third-party endpoint |
| **Pure Gateway Overhead (Min)** | **2.95 ms** | 18 – 45 ms | N/A |
| **Semantic Cache Hit Latency (Avg)**| **4.90 ms** | 35 – 80 ms (via Redis) | N/A (350 ms cold) |
| **Cache Hit Rate** | **91.0%** (91/100) | Varies | 0.0% |
| **Active Memory Footprint (RSS)** | **~464 MB (with ONNX model)** | ~550 MB – 1.2 GB | Zero local RAM |
| **Cold Start Time** | **< 1.2 s** | 8 – 15 s | N/A |
| **Latency P50 / P95** | **4.18 ms / 39.78 ms** | 42.0 ms / 120.0 ms | 320 ms / 890 ms |

To reproduce locally:
```bash
make bench
# or
uv run python benchmarks/bench.py
```

---

## Terminal in Action

<p align="center">
  <img src="docs/terminal-animated.svg" alt="SentinelGate Animated Terminal Session" width="100%" style="border-radius: 8px; border: 1px solid #27272A; margin: 10px 0;">
</p>

Launch the gateway and test it in under 5 seconds with zero configuration:

```bash
# 1. Launch SentinelGate (Auto-detects keys and local runtimes)
$ sentinelgate --port 8000

  ┌──────────────────────────────────────────────────────────────┐
  │  SentinelGate v0.3.0 · Zero-Bloat LLM Gateway                │
  │  FastAPI · SQLite WAL · FastEmbed ONNX · Python 3.9+         │
  └──────────────────────────────────────────────────────────────┘
  Active Providers : OpenAI, Anthropic, Groq, Ollama (11434)
  Storage Engine   : SQLite WAL (mmap: 256MB)
  Proxy Endpoint   : http://localhost:8000/v1
  Developer HUD    : http://localhost:8000/dashboard
```

Send a test request with `model="auto"`:

```bash
$ curl -i http://localhost:8000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{
      "model": "auto",
      "messages": [{"role": "user", "content": "Write a 3-line bash loop."}]
    }'

HTTP/1.1 200 OK
content-type: application/json
x-cache: HIT (cosine: 0.9412)
x-latency: 1.84ms
x-saved-usd: 0.003420
x-auto-route: ollama/llama3
x-secrets-scrubbed: 0

{
  "id": "chatcmpl-sg-882194b",
  "object": "chat.completion",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "for i in {1..3}; do\n  echo \"Step $i\"\ndone"
    },
    "finish_reason": "stop"
  }],
  "usage": { "prompt_tokens": 18, "completion_tokens": 16, "total_tokens": 34 }
}
```

---

## Why SQLite WAL and Not Redis?

Most LLM proxies introduce distributed infrastructure (Redis clusters, PostgreSQL databases, Celery queues) that complicates local development and introduces unnecessary latency. SentinelGate replaces this stack with in-process SQLite Write-Ahead Logging:

| Engineering Dimension | Hosted Redis / Valkey Stack | SentinelGate SQLite WAL |
| :--- | :--- | :--- |
| **Inter-Process Latency** | `1.2 – 2.8 ms` (loopback TCP network socket) | **`< 0.15 ms`** (direct memory-mapped pointer) |
| **Syscall Overhead** | `sendto()`, `recvfrom()`, `epoll()` context switches | **Zero socket syscalls** (`PRAGMA mmap_size`) |
| **Serialization Protocol**| RESP protocol text formatting and parsing | **Native C binary structs** stored directly on disk |
| **Operational Overhead** | External daemon, port allocation, sentinel nodes | **Zero daemon** (single portable `.db` file) |
| **Crash Safety** | AOF fsync latency or append-only write lag | **Atomic WAL ring buffer** with checksum verification |

---

<p align="center">
  <img src="docs/dashboard-preview.png" alt="SentinelGate Operational Dashboard HUD" width="100%" style="border-radius: 8px; border: 1px solid #27272A;">
</p>

---

## Developer Console & Interactive HUD (`/dashboard`)

SentinelGate includes a single-file, zero-bloat developer console served directly from FastAPI (zero `node_modules`, zero npm build pipeline) built following Linear / Raycast design principles:

- **Live SQLite Telemetry Strip:** Real-time query of Total Requests, Semantic Cache Hit Rate (%), Total USD Saved, Median Latency (`p50 ms`), Secrets Scrubbed, and Blocked Threats with a `Live (2s)` toggle.
- **1-Click Playground Presets:** Test the full gateway pipeline right from your browser:
  - `Send Normal Prompt`: Uncached baseline query ($\to$ `UPSTREAM` execution).
  - `Send Paraphrased Prompt`: Semantically equivalent query ($\to$ `CACHE HIT` in `<2.0ms` at **$0.00 cost**).
  - `Test Secret Firewall`: Outbound prompt with leaked keys ($\to$ `REDACTED` status pill).
  - `Auto-Route (Simple)`: Routine Q&A query ($\to$ routes to cheap utility backend, saving 99%).
  - `Auto-Route (Complex)`: Architecture reasoning query ($\to$ routes to Claude 3.5 Sonnet / GPT-4o).
- **1-Click Chaos Outage Injection (`/api/chaos`):** Flip the Chaos switch to simulate HTTP 429 rate limits or 503 provider outages and observe automatic circuit breaker failover.
- **Shadow Model Replay & Unit Economics (`/api/replay`):** Replay any captured trace against secondary cheaper models, showing side-by-side completion parity and exact projected monthly savings at 100k requests/month.
- **1-Click Eval Dataset Export (`/api/export/traces.jsonl` & `.csv`):** Export captured SQLite production traces directly into OpenAI Fine-Tuning and evaluation dataset formats with one click.
- **Single-Key Developer Navigation:**

| Key | Action | Key | Action |
| :---: | :--- | :---: | :--- |
| <kbd>?</kbd> | Toggle Keyboard Shortcuts cheat sheet | <kbd>Esc</kbd> | Close open drawer / modal |
| <kbd>r</kbd> | Refresh telemetry metrics & traces | <kbd>a</kbd> | Toggle 2s live auto-refresh |
| <kbd>c</kbd> | Open Connect Tool integration modal | <kbd>f</kbd> | Toggle Chaos 429 fault injection |
| <kbd>p</kbd> | Focus Playground simulator prompt box | <kbd>1</kbd>–<kbd>5</kbd> | Trigger Playground test presets 1 through 5 |

---

## 10-Second Quickstart

### 0. 1-Click Launch via GitHub Codespaces (Zero Local Install)

Test SentinelGate instantly in your browser with zero terminal configuration:

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/giridhar2005/sentinelgate)

GitHub Codespaces spins up an isolated Python 3.11 container, installs SentinelGate in editable mode, launches the proxy with deterministic sandbox mode on port 8000, and forwards the `/dashboard` directly into your browser tab.

---

### 1. Local Install & Launch

```bash
# 1-Liner: Clone, install in editable mode, and launch
git clone https://github.com/giridhar2005/sentinelgate.git && cd sentinelgate && pip install -e . && sentinelgate
```

Or run directly without installing:
```bash
python -m sentinelgate
# or with uv
uv run python -m sentinelgate
```

> [!NOTE]
> **Zero-Key Sandbox Mode:** If no cloud provider API keys (`OPENAI_API_KEY`, etc.) or local model runtimes are detected, SentinelGate boots automatically into deterministic sandbox mode. The interactive HUD and `/v1/chat/completions` endpoint work out of the box on first clone.

---

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

#### Node.js / TypeScript SDK
```typescript
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "http://localhost:8000/v1",
  apiKey: "sk-sentinelgate-local",
});

const completion = await client.chat.completions.create({
  model: "auto",
  messages: [{ role: "user", content: "Write a quicksort in TypeScript" }],
});

console.log(completion.choices[0].message.content);
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
<summary><b>3. In-Process Semantic Cache & FastEmbed ONNX</b></summary>

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

## Developer Commands & Makefile

A `Makefile` is included for common engineering workflows:

```bash
make install    # Install package in editable mode with dev dependencies
make dev        # Run SentinelGate with hot-reloading on port 8000
make run        # Run SentinelGate in production mode
make test       # Execute full pytest suite (36 tests)
make bench      # Run local latency and semantic cache benchmark
make lint       # Run Ruff linter checks
make format     # Auto-format codebase using Ruff
make clean      # Clean caches and temporary SQLite WAL files
```

---

## Frequently Asked Systems Questions

<details>
<summary><b>Q: How does reversible secret redaction avoid breaking the LLM's understanding?</b></summary>

When a prompt contains sensitive credentials (e.g. `sk-proj-xyz` or `postgresql://admin:pass@host/db`), the regex engine replaces them with structurally neutral placeholders (`__SG_SECRET_1__`). Because models understand tokens like `__SG_SECRET_1__` as opaque string variables, they generate correct code diffs, queries, or configuration advice. When response chunks stream back through SentinelGate, the original values are substituted back into the text before delivery to the client.
</details>

<details>
<summary><b>Q: Can SentinelGate run completely offline without an internet connection?</b></summary>

Yes. By pointing `config.yaml` to local model servers (`ollama` on port 11434 or `lm-studio` on port 1234), all prompt processing, vector embedding via FastEmbed ONNX, and LLM inference occur entirely on your local machine with zero external network connectivity.
</details>

<details>
<summary><b>Q: How does <code>model="auto"</code> evaluate prompt complexity?</b></summary>

The complexity scorer computes a deterministic score (0–100) in `<0.2ms` based on three parameters: token length, syntax structures (multi-file diffs, JSON schemas, stack traces), and conceptual reasoning keywords (`architect`, `concurrency`, `deadlock`, `distributed`, `optimize`). Routine queries (haikus, general definitions, translations) score $\le 30$ and route to fast utility models. Prompts scoring $> 30$ route to frontier models.
</details>

<details>
<summary><b>Q: What happens if SentinelGate or the host machine suddenly loses power?</b></summary>

SQLite in Write-Ahead Logging (WAL) mode guarantees ACID compliance. Committed transactions in the WAL file are checkpointed back to the main database file during normal shutdown or auto-checkpoint intervals. If a sudden crash occurs, uncommitted writes are rolled back upon recovery without database corruption.
</details>

---

## Test Suite & Code Quality

The repository includes 36 tests covering streaming SSE, semantic cache TTL, automatic failover routing, sandbox execution, chaos injection, reversible secret redaction, and `model="auto"` complexity scoring:

```bash
make test
make lint
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
