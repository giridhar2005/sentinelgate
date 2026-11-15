from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

import httpx
import pytest
import respx
import yaml
from starlette.testclient import TestClient

from sentinelgate.main import create_app


@pytest.fixture
def integration_app(tmp_path):
    db_file = str(tmp_path / "integration_test.db")
    cfg_file = str(tmp_path / "config.yaml")
    config_dict = {
        "server": {"host": "127.0.0.1", "port": 8000, "admin_token": "sg-integration-admin"},
        "database": {"path": db_file},
        "routing": {"default_model": "gpt-4o-mini", "enable_cache": True},
        "security": {"secret_action": "redact"},
        "budget": {"daily_limit_usd": 100.0},
        "providers": {
            "openai": {
                "type": "openai",
                "base_url": "https://api.openai.com/v1",
                "api_keys": ["sk-openai-live-key"],
                "models": {
                    "gpt-4o-mini": {
                        "input_cost_per_m": 0.15,
                        "output_cost_per_m": 0.60,
                        "fallbacks": ["groq/llama-3.1-8b-instant"],
                    },
                    "gpt-4o": {
                        "input_cost_per_m": 2.50,
                        "output_cost_per_m": 10.0,
                        "fallbacks": ["groq/llama-3.3-70b-versatile"],
                    },
                },
            },
            "groq": {
                "type": "openai",
                "base_url": "https://api.groq.com/openai/v1",
                "api_keys": ["gsk-groq-live-key"],
                "models": {
                    "llama-3.1-8b-instant": {
                        "input_cost_per_m": 0.05,
                        "output_cost_per_m": 0.08,
                        "fallbacks": [],
                    },
                    "llama-3.3-70b-versatile": {
                        "input_cost_per_m": 0.59,
                        "output_cost_per_m": 0.79,
                        "fallbacks": [],
                    },
                },
            },
        },
    }
    with open(cfg_file, "w") as f:
        yaml.dump(config_dict, f)

    app = create_app(config_path=cfg_file, db_path=db_file)
    client = TestClient(app)
    return client, app


@respx.mock
def test_integration_happy_path_auto_routing(integration_app):
    client, app = integration_app
    app.state.router.client = None

    # Mock OpenAI frontier upstream for complex prompt
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-happy-auto",
                "object": "chat.completion",
                "model": "gpt-4o",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "To architect a high-throughput distributed state machine, apply consensus logs.",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60},
            },
        )
    )

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sg-integration-admin"},
        json={
            "model": "auto",
            "messages": [
                {
                    "role": "user",
                    "content": "Architect a distributed state machine with Raft consensus and high concurrency benchmarks.",
                }
            ],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "distributed state machine" in data["choices"][0]["message"]["content"]
    assert resp.headers["X-Cache"] == "MISS"
    assert "openai" in resp.headers["X-Auto-Route"]
    assert "complex" in resp.headers["X-Auto-Complexity"]


@respx.mock
def test_integration_primary_500_failover_to_secondary(integration_app):
    client, app = integration_app
    app.state.router.client = None

    # OpenAI fails with 500
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    # Groq fallback succeeds with 200
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-groq-fallback",
                "object": "chat.completion",
                "model": "llama-3.1-8b-instant",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "Recovered via Groq secondary fallback.",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 15, "completion_tokens": 10, "total_tokens": 25},
            },
        )
    )

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sg-integration-admin"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "Explain connection failover."}],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "Recovered via Groq" in data["choices"][0]["message"]["content"]
    assert resp.headers["X-Provider-Used"] == "groq"


@pytest.mark.asyncio
async def test_integration_speculative_hedging_primary_slow(integration_app):
    _, app = integration_app

    async def mock_execute_request(req_data: Dict[str, Any], stream: bool = False):
        m = req_data.get("model", "")
        if "gpt-4o" in m:
            # Primary slow
            await asyncio.sleep(0.60)
            return (
                {
                    "id": "chatcmpl-primary-slow",
                    "choices": [{"message": {"content": "Slow primary finished"}}],
                },
                "openai",
                "gpt-4o-mini",
            )
        else:
            # Fast hedge
            await asyncio.sleep(0.02)
            return (
                {
                    "id": "chatcmpl-hedge-fast",
                    "choices": [{"message": {"content": "Fast hedge finished"}}],
                },
                "groq",
                "llama-3.1-8b-instant",
            )

    app.state.router.execute_request = mock_execute_request

    res, p, m, meta = await app.state.router.execute_hedged_request(
        {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Latency test"}]},
        hedge_delay_sec=0.10,
    )
    assert meta["hedged_triggered"] is True
    assert meta["winner"] == "hedge"
    assert "Fast hedge finished" in res["choices"][0]["message"]["content"]


@respx.mock
def test_integration_tool_call_blocked_by_guardrail(integration_app):
    client, app = integration_app
    app.state.router.client = None

    # Upstream attempts to return a dangerous tool call (rm -rf /)
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-danger-tool",
                "object": "chat.completion",
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_rm_123",
                                    "type": "function",
                                    "function": {
                                        "name": "run_command",
                                        "arguments": json.dumps({"command": "rm -rf /"}),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 12, "total_tokens": 22},
            },
        )
    )

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sg-integration-admin"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "Clean the disk."}],
        },
    )
    assert resp.status_code == 200
    assert resp.headers["X-Agent-Firewall-Blocked"] == "true"
    data = resp.json()
    # Tool call stripped and replaced with refusal
    assert data["choices"][0]["message"]["tool_calls"] is None
    assert (
        "SentinelGate Agent Security Firewall blocked tool execution"
        in data["choices"][0]["message"]["content"]
    )


@respx.mock
def test_integration_streaming_response_secret_redacted_midstream(integration_app):
    client, app = integration_app
    app.state.router.client = None

    # Stream SSE chunks with an OpenAI secret split across chunks
    raw_sse = (
        'data: {"id":"chk1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"Here is the key: sk-"},"finish_reason":null}]}\n\n'
        'data: {"id":"chk2","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"proj-abcdef12345678901234567890123456"},"finish_reason":null}]}\n\n'
        'data: {"id":"chk3","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":" - keep it safe."},"finish_reason":null}]}\n\n'
        "data: [DONE]\n\n"
    )

    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=raw_sse.encode("utf-8"),
        )
    )

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sg-integration-admin"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "Show me a key."}],
            "stream": True,
        },
    )
    assert resp.status_code == 200
    body = resp.text
    # Secret must never appear in raw streamed output
    assert "sk-proj-abcdef" not in body
    assert "[REDACTED_SECRET" in body or "__SG_SECRET_" in body


def test_integration_key_budget_exceeded_429(integration_app):
    client, app = integration_app

    # Test 1: Virtual Key RPM rate limit triggers 429
    vk_mgr = app.state.virtual_keys
    key_info = vk_mgr.create_key(
        name="low-rpm-test",
        rate_limit_rpm=1,
        rate_limit_tpm=100000,
    )
    raw_key = key_info["key"]

    # First request: uses the 1 permitted RPM slot
    ok, err, hdrs = vk_mgr.verify_and_rate_limit(raw_key, estimated_tokens=10)
    assert ok is True

    # Second request with the same virtual key: hits 429 rate limit
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "Hello budget test"}],
        },
    )
    assert resp.status_code == 429
    data = resp.json()
    assert "rate limit exceeded" in data["error"]["message"].lower()

    # Test 2: Global spend budget breach triggers 429
    app.state.budget.record_spend(150.0)  # limit is 100.0
    resp_budget = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sg-integration-admin"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "Hello budget test"}],
        },
    )
    assert resp_budget.status_code == 429
    data_budget = resp_budget.json()
    assert "budget limit exceeded" in data_budget["error"]["message"].lower()
