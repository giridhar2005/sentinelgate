# SentinelGate Configuration Reference

SentinelGate reads configuration from `config.yaml` at startup. If `config.yaml` is absent, it falls back to defaults defined in `config.example.yaml`.

---

## 1. Example Configuration

```yaml
server:
  host: "0.0.0.0"
  port: 8000
  workers: 1
  log_level: "info"

cache:
  enabled: true
  similarity_threshold: 0.85
  ttl_seconds: 86400
  embedding_model: "BAAI/bge-small-en-v1.5"

budget:
  daily_limit_usd: 50.0
  alert_webhook_url: null

routing:
  hedge_delay_ms: 450
  auto_route_threshold: 30
  fallback_model: "llama-3.1-8b-instant"

guardrails:
  secret_action: "redact"  # "redact" or "block"
  block_shell_commands: true
  block_sql_mutations: true
  block_ssrf: true

providers:
  openai:
    base_url: "https://api.openai.com/v1"
    api_key: "${OPENAI_API_KEY}"
  groq:
    base_url: "https://api.groq.com/openai/v1"
    api_key: "${GROQ_API_KEY}"
```

---

## 2. Configuration Parameters

### `server`
- `host` (string): Bind interface (default: `"0.0.0.0"`).
- `port` (integer): Listening port (default: `8000`).
- `workers` (integer): Number of Uvicorn ASGI worker processes (default: `1`).

### `cache`
- `enabled` (boolean): Enables or disables the in-process semantic cache.
- `similarity_threshold` (float): Minimum cosine similarity ($0.0$ to $1.0$) between incoming prompt embedding and cached prompt embedding to trigger a cache hit (default: `0.85`).
- `ttl_seconds` (integer): Time-to-live for cached responses in seconds (default: `86400` / 24 hours).
- `embedding_model` (string): FastEmbed ONNX embedding model loaded locally on CPU (default: `BAAI/bge-small-en-v1.5`).

### `budget`
- `daily_limit_usd` (float): Global ceiling on daily upstream provider expenditures across all tenants. When exceeded, the gateway responds with HTTP 429 budget exhaustion errors.
- `alert_webhook_url` (string, optional): HTTP webhook endpoint notified when spend crosses 80% and 100% of the daily limit.

### `routing`
- `hedge_delay_ms` (integer): Milliseconds to wait before speculatively dispatching a secondary request to a fast fallback provider (default: `450`).
- `auto_route_threshold` (integer): Prompt complexity threshold (0 to 100). Prompts scoring below this threshold are routed to lightweight, lower-cost models.
- `fallback_model` (string): Target model ID used for speculative hedging and provider failover.

### `guardrails`
- `secret_action` (string): Action taken when API keys, tokens, or credentials are found in requests/responses (`"redact"` to replace with placeholders or `"block"` to reject with HTTP 400).
- `block_shell_commands` (boolean): Scans tool-call arguments for destructive shell commands (`rm -rf`, pipe-to-sh, reverse shells).
- `block_sql_mutations` (boolean): Scans tool-call arguments for destructive SQL statements (`DROP TABLE`, `TRUNCATE`, unconstrained deletions).
- `block_ssrf` (boolean): Inspects outbound tool call destinations and MCP proxy targets to reject RFC 1918 private IPs, link-local addresses, and loopbacks.

---

## 3. Virtual API Keys & Rate Limiting

SentinelGate implements token-bucket rate limiting stored directly in SQLite WAL:

- **RPM (Requests Per Minute):** Sliding window counter tracking active request frequency.
- **TPM (Tokens Per Minute):** Approximate token budget tracking.
- **Daily Spend Allocation:** Optional per-key spending limits in USD.

Virtual keys can be created using the CLI or through `/dashboard`:

```bash
sentinelgate create-key --name "agent-local" --rpm 60 --tpm 100000
```
