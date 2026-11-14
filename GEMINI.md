# SentinelGate - Project Rules & Architecture Guidelines

## 1. Strict Tech Stack & Package Rules
- **Package Manager**: Exclusively use `uv` (`uv init`, `uv add`, `uv run`). Never use `pip`.
- **FORBIDDEN Libraries**:
  - `openai`
  - `anthropic`
  - `google-genai`
  - `langchain`
  - `litellm`
  - `torch`
  - `sentence-transformers`
- **ALLOWED Dependencies**:
  - `fastapi`
  - `uvicorn`
  - `httpx`
  - `pydantic`
  - `pyyaml`
  - `numpy`
  - `fastembed`
  - `pytest`
  - `respx`
  - `ruff`
  - Built-in Python standard libraries (e.g., `sqlite3`, `re`, `json`, `time`, `uuid`, etc.)

## 2. Core Code Constraints
- **Compact & Zero-Bloat**: Core source code must remain under 1,500 lines across all modules.
- **Provider Protocols**: Direct REST API integration with Google Gemini, Groq, OpenAI, and Ollama using `httpx.AsyncClient` with connection pooling and streaming SSE support.
- **Failover & Key Rotation**: Round-robin API key rotation per provider with automatic failover to backup models on HTTP 429 and 503.
- **Semantic Cache**: Cosine similarity via NumPy against cached responses in SQLite (`sentinelgate.db`) with `fastembed` (lightweight ONNX). Similarity threshold > 0.92, TTL support, and `X-No-Cache: true` bypass.
- **Guardrails**: Mask emails, phone numbers, and API keys with reversible placeholders before sending upstream, restoring them in the final output. 1-shot self-repair retry for invalid JSON responses when `response_format={"type": "json_object"}` is requested.
- **Observability**: SQLite metrics logging (latency, tokens, cost, cache status), 10% shadow evals with LLM-as-a-judge quality parity scoring, and dark-mode web dashboard at `/dashboard`.
- **Offline First**: All test suites must run 100% offline without real API keys using `pytest` and `respx`.
