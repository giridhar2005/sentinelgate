"""Quickstart showing drop-in OpenAI SDK compatibility with SentinelGate."""

import os
from openai import OpenAI

# SentinelGate exposes standard OpenAI-compatible endpoints at /v1.
# You can pass your own virtual key or any placeholder key for local testing.
client = OpenAI(
    base_url=os.getenv("SENTINELGATE_BASE_URL", "http://localhost:8000/v1"),
    api_key=os.getenv("SENTINELGATE_API_KEY", "sg-live-demo-key"),
)

print("=== 1. Non-Streaming Completion ===")
completion = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[
        {"role": "system", "content": "You are a concise systems engineer."},
        {"role": "user", "content": "Explain the difference between TCP and UDP in one sentence."},
    ],
)
print(completion.choices[0].message.content)

print("\n=== 2. Streaming Completion (SSE) ===")
stream = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[
        {"role": "user", "content": "List 3 advantages of SQLite in bullet points."},
    ],
    stream=True,
)
for chunk in stream:
    delta = chunk.choices[0].delta.content
    if delta:
        print(delta, end="", flush=True)
print()
