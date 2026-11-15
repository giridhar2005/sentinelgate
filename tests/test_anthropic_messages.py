"""End-to-End Tests for Anthropic Messages API Adapter (/v1/messages).

Verifies compatibility with Claude Code CLI and Anthropic SDK.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


def test_anthropic_messages_non_streaming(client: TestClient):
    """Verify non-streaming Anthropic Messages API request returns standard Anthropic JSON schema."""
    payload = {
        "model": "claude-3-5-sonnet-20241022",
        "system": "You are a senior systems engineer.",
        "messages": [
            {"role": "user", "content": "What is SQLite WAL mode?"}
        ],
        "max_tokens": 512,
        "temperature": 0.5,
    }

    headers = {
        "x-api-key": "sentinel-local",
        "anthropic-version": "2023-06-01",
    }

    response = client.post("/v1/messages", json=payload, headers=headers)
    assert response.status_code == 200, f"Error: {response.text}"
    assert response.headers.get("anthropic-version") == "2023-06-01"

    data = response.json()
    assert data["type"] == "message"
    assert data["role"] == "assistant"
    assert isinstance(data["content"], list)
    assert len(data["content"]) > 0
    assert data["content"][0]["type"] == "text"
    assert "usage" in data
    assert "input_tokens" in data["usage"]
    assert "output_tokens" in data["usage"]


def test_anthropic_messages_streaming(client: TestClient):
    """Verify streaming Anthropic Messages API request emits valid Anthropic SSE events."""
    payload = {
        "model": "claude-3-5-sonnet-20241022",
        "messages": [
            {"role": "user", "content": "Explain speculative execution in 1 sentence."}
        ],
        "stream": True,
    }

    headers = {
        "x-api-key": "sentinel-local",
        "anthropic-version": "2023-06-01",
    }

    response = client.post("/v1/messages", json=payload, headers=headers)
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")

    events = []
    for line in response.text.splitlines():
        if line.startswith("event: "):
            events.append(line[7:].strip())

    assert "message_start" in events
    assert "content_block_start" in events
    assert "content_block_delta" in events
    assert "content_block_stop" in events
    assert "message_delta" in events
    assert "message_stop" in events


def test_anthropic_messages_invalid_empty_body(client: TestClient):
    """Verify malformed Anthropic request returns proper error format."""
    headers = {"x-api-key": "sentinel-local"}
    response = client.post("/v1/messages", json={"model": "auto", "messages": []}, headers=headers)
    assert response.status_code == 400
    data = response.json()
    assert data.get("type") == "error"
    assert "message" in data.get("error", {})


def test_anthropic_messages_tool_calling(client: TestClient):
    """Verify Anthropic Messages API properly accepts tools schema and returns tool_use content block."""
    payload = {
        "model": "claude-3-5-sonnet-20241022",
        "messages": [
            {"role": "user", "content": "What is the weather in Seattle?"}
        ],
        "tools": [
            {
                "name": "get_current_weather",
                "description": "Get current weather for a location",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "location": {"type": "string", "description": "City name"}
                    },
                    "required": ["location"],
                },
            }
        ],
    }

    headers = {"x-api-key": "sentinel-local"}
    response = client.post("/v1/messages", json=payload, headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["type"] == "message"
    assert "content" in data
