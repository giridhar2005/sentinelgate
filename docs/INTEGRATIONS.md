# SentinelGate Client & IDE Integrations

SentinelGate exposes OpenAI-compatible and Anthropic-compatible endpoints. Any client, editor, or agent framework supporting custom base URLs can route through SentinelGate.

---

## 1. Editor & Coding Agent Integrations

### Cursor / Cline / Continue.dev

In your editor or extension settings, configure custom OpenAI provider settings:

- **Base URL:** `http://localhost:8000/v1`
- **API Key:** `sentinel-local` (or a virtual key created via SentinelGate CLI / dashboard)
- **Model:** `auto` (or explicit target models such as `gpt-4o-mini`, `claude-3-5-sonnet`)

Requests will automatically pass through SentinelGate's local cache, credential redactor, and tool guardrails.

### Claude Code CLI

SentinelGate supports the Anthropic Messages API at `/v1/messages`. You can direct Claude Code to SentinelGate by exporting environment variables:

```bash
export ANTHROPIC_BASE_URL="http://localhost:8000/v1"
export ANTHROPIC_API_KEY="sentinel-local"
claude
```

SentinelGate translates incoming Messages requests into internal representation, applies caching and guardrails, routes to the upstream provider, and returns streaming or non-streaming responses matching Anthropic's schema.

---

## 2. Local Model Runtimes (Ollama & LM Studio)

SentinelGate automatically detects local LLM runtimes on startup:

- **Ollama:** `http://127.0.0.1:11434`
- **LM Studio:** `http://127.0.0.1:1234`

When local servers are detected and configured in `config.yaml`, SentinelGate routes prompts locally with zero cloud egress cost:

```yaml
providers:
  ollama:
    base_url: "http://127.0.0.1:11434/v1"
    api_key: "ollama"
    models: ["llama3.2", "qwen2.5-coder"]
```

---

## 3. SDK Code Examples

### Python (OpenAI SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="sentinel-local",
)

response = client.chat.completions.create(
    model="auto",
    messages=[
        {"role": "system", "content": "You are a backend assistant."},
        {"role": "user", "content": "How do database connection pools work?"},
    ],
    temperature=0.2,
)

print(response.choices[0].message.content)
```

### Python (Anthropic SDK)

```python
import anthropic

client = anthropic.Anthropic(
    base_url="http://localhost:8000",
    api_key="sentinel-local",
)

message = client.messages.create(
    model="claude-3-5-sonnet-20241022",
    max_tokens=1024,
    messages=[
        {"role": "user", "content": "Explain write-ahead logging."},
    ],
)

print(message.content[0].text)
```

### Node.js / TypeScript (OpenAI SDK)

```typescript
import OpenAI from "openai";

const openai = new OpenAI({
  baseURL: "http://localhost:8000/v1",
  apiKey: "sentinel-local",
});

async function main() {
  const completion = await openai.chat.completions.create({
    model: "auto",
    messages: [{ role: "user", content: "Summarize HTTP/2 multiplexing" }],
  });
  console.log(completion.choices[0].message.content);
}

main();
```

---

## 4. cURL Requests

### OpenAI Chat Completion (`/v1/chat/completions`)

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sentinel-local" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [
      {"role": "user", "content": "Ping"}
    ]
  }'
```

### Anthropic Messages (`/v1/messages`)

```bash
curl -X POST http://localhost:8000/v1/messages \
  -H "x-api-key: sentinel-local" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-3-5-sonnet-20241022",
    "max_tokens": 512,
    "messages": [
      {"role": "user", "content": "Hello"}
    ]
  }'
```
