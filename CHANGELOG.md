# Changelog

All notable changes to SentinelGate will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.3.0] - 2026-11-14

### Added
- **Universal Zero-Config Auto-Discovery & CLI:**
  - Registered `sentinelgate` console entrypoint in `pyproject.toml` (`sentinelgate` / `python -m sentinelgate`).
  - Runtime probe for local runtimes (Ollama at `11434`, LM Studio at `1234`) and cloud environment variables (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`).
  - Zero-Key Sandbox Mode: Deterministic local simulation boots automatically when no keys or local models exist, keeping the HUD and `/v1/chat/completions` functional on first clone.
- **Universal Smart Cost-Router (`model="auto"`):**
  - Deterministic, sub-millisecond prompt complexity scorer evaluating token length, syntax structure, and reasoning/architectural keywords.
  - Automatically arbitrates simple Q&A to cheap utility backends (Ollama, Groq, GPT-4o-mini) and complex prompts to frontier models (Claude 3.5 Sonnet, GPT-4o).
  - Telemetry headers: `X-Auto-Route`, `X-Auto-Complexity`, and `X-Auto-Saved-USD` quantifying exact cost arbitrage vs. frontier baselines.
- **Reversible Secret & Credential Redaction Shield:**
  - Sub-millisecond compiled regex firewall scanning outbound prompts for API keys (OpenAI, Anthropic, AWS, GitHub), JWTs, database URIs (`postgresql://`, `mysql://`), and SSNs.
  - Dual operational modes: `redact` (replaces secrets with reversible `__SG_SECRET_X__` placeholders outbound and hydrates them in response chunks inbound) and `block` (strict HTTP 400 rejection).
  - Telemetry headers: `X-Secrets-Scrubbed` and HUD badge `REDACTED`.
- **HUD Developer Console Polish & Telemetry:**
  - Single-key developer navigation (`?` cheat sheet, `Esc` drawer/modal close, `r` refresh, `a` auto-refresh, `c` connect modal, `f` chaos toggle, `1-5` playground presets, `p` prompt simulator focus).
  - Floating toast notification system replacing browser alert dialogues.
  - Connect Tool modal with copy-paste snippets for Cursor/Cline, CLI environments, Python SDK, and Node.js.
  - 1-Click Chaos Outage Injection (`/api/chaos`) simulating HTTP 429/503 primary failures to demonstrate circuit breaker failover.
  - Shadow Model Replay & Unit Economics Drawer (`/api/replay`) calculating projected monthly USD savings at 100k requests/month.
  - 1-Click Eval Dataset Export (`/api/export/traces.jsonl` & `.csv`) for streaming export of SQLite traces in OpenAI fine-tuning and eval formats.
- **Animated Visuals & 1-Click Cloud Sandbox:**
  - Handcrafted `docs/terminal-animated.svg` session with CSS keyframe animation demonstrating live CLI startup and cURL query execution.
  - Pre-configured `.devcontainer/devcontainer.json` enabling 1-click cloud sandbox testing via GitHub Codespaces with zero local setup.
- **Systems RFC Documentation & CI Matrix:**
  - 7-section systems RFC in `ARCHITECTURE.md` analyzing in-process SQLite WAL vs. hosted Redis, IPC syscall overhead, memory-mapped pages, and tail latency hedging.
  - GitHub Actions CI workflow testing Python 3.9, 3.10, 3.11, and 3.12 with Ruff linting and Pytest.
  - GitHub issue forms (`bug_report.yml`, `feature_request.yml`), PR template, `SECURITY.md`, `CONTRIBUTING.md`, and `CITATION.cff`.

### Changed
- Refactored `router.score_complexity` to enforce regex word boundaries `\b\w+\b` preventing accidental substring matches on technical terms.
- Expanded SQLite WAL telemetry queries to report `Secrets Scrubbed` count across all transactions.

---

## [0.2.0] - 2026-11-14

### Added
- **In-Process Semantic Cache:**
  - Dense 384-dimensional vector embedding generation on CPU using FastEmbed (`BAAI/bge-small-en-v1.5`).
  - Cosine similarity matching threshold ($\ge 0.92$) serving cached completions in `<2.0ms` at **$0.00 cost**.
  - Cache bypass support via `Cache-Control: no-cache` and `X-No-Cache: true` headers.
- **SQLite WAL Storage Engine:**
  - Zero-bloat database layer using Write-Ahead Logging (`PRAGMA journal_mode = WAL;`) and memory mapping (`PRAGMA mmap_size = 268435456;`).
  - High concurrency support for simultaneous reader threads with serialized single-writer queues.
- **Circuit Breaker & Automatic Failover:**
  - Sliding-window provider health tracking with 30-second cooldown states.
  - Seamless failover across primary (`openai`) and backup providers (`groq`, `gemini`, `ollama`) on HTTP 429 or 5xx responses.
- **Streaming SSE Proxy:**
  - Server-Sent Events (SSE) streaming engine with chunk forwarding, keep-alive frames, and backpressure management.
- **Virtual Tenant Keys:**
  - Self-contained tenant keys (`sg-...`) with token-bucket rate limits (RPM) and monthly spend ceilings stored in SQLite.

---

## [0.1.0] - 2026-11-14

### Added
- Initial project architecture with FastAPI and async HTTPX client.
- OpenAI-compatible `/v1/chat/completions` and `/v1/models` proxy routing.
- Basic YAML configuration parser and mock upstream fallbacks.
