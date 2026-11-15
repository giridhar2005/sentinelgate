# Changelog

All notable changes to SentinelGate will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.0] - 2026-11-15

### Added
- **Unified Gateway Pipeline & Dual Protocol Adapters:**
  - Single execution path for OpenAI Chat Completions (`POST /v1/chat/completions`) and Anthropic Messages API (`POST /v1/messages`).
  - Anthropic Server-Sent Events (SSE) compliant streaming (`message_start`, `content_block_start`, `content_block_delta`, `message_delta`, `message_stop`).
- **Deterministic Agent Tool-Call & MCP Security Firewall:**
  - Pre-execution tool-call inspection for shell commands via lexical `shlex` parsing, flag normalization, and variable expansion.
  - Pre-execution SQL AST inspection via `sqlglot` blocking destructive mutations (`DROP`, `TRUNCATE`, unguarded `DELETE`).
  - Network tool argument inspection blocking SSRF targets (loopback, private RFC 1918, link-local, CGNAT, cloud metadata `169.254.169.254`).
  - Scoped argument inspection excluding file-writing and editing content from false-positive shell/SQL detection.
  - Evaluated against 333-sample adversarial and benign test corpus in `tests/corpus/` with 100% recall and 100% precision.
- **Unified Authentication & Virtual Key Management:**
  - Constant-time token verification accepting either admin tokens or provisioned virtual keys (`sg-*`).
  - Virtual keys stored hashed at rest using SHA-256 with a server-side pepper.
  - Localhost-only restriction for keyless `--demo` mode with refusal to start if cloud keys are present.
  - Origin-locked CORS and per-IP failure tracking with temporary lockout.
- **Streaming Reversible Secret Redaction:**
  - Sliding-window byte stream redactor buffering chunk boundaries across token splits.
  - Reversible outbound secret tokenization and response chunk rehydration.
  - Property-tested across 500 randomized chunk boundary splits with Hypothesis.
- **In-Process Semantic Caching & State Engine:**
  - SQLite WAL mode with 256MB memory mapping (`PRAGMA mmap_size=268435456`) and sub-millisecond read access.
  - Embedded FastEmbed ONNX runtime (`BAAI/bge-small-en-v1.5`) with exact-match fallback on `/health`.
  - Multi-tenant cache key isolation partitioning by tenant ID, model, system prompt hash, tools hash, and temperature.
  - Strict exclusion of tool-call responses from cache storage.
- **Routing & Resilience:**
  - Sub-millisecond deterministic heuristic prompt classifier (`model="auto"`).
  - Upstream provider failover with provider circuit breaker.
  - Speculative latency hedging racing primary providers against fast fallbacks on latency timeout with loser task cancellation.
  - Dual-bucket sliding-window rate limiting (RPM + TPM) and daily USD budget caps per virtual key.
- **Observability & Developer HUD:**
  - Obsidian-themed HUD (`/dashboard`) for operational metrics, trace inspection, and manual chaos testing.
  - Native Prometheus RFC 0004 text exposition (`GET /metrics`).
  - Non-blocking background telemetry queue writing request traces and token metrics asynchronously.
- **Honest & Reproducible Benchmarking:**
  - Real HTTP benchmark harness (`benchmarks/bench.py`) executing against in-process Uvicorn gateway over loopback.
  - Speculative latency hedging simulation harness (`benchmarks/bench_hedging.py`).
  - Benchmark report generator (`scripts/generate_benchmarks_doc.py`) validating figures strictly against committed JSON telemetry.
