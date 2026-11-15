from __future__ import annotations

import asyncio
import json
import os
import tempfile
from unittest.mock import patch

import httpx
import pytest
import respx
import yaml
from starlette.testclient import TestClient

from sentinelgate.main import create_app
from sentinelgate.services.cache import SemanticCache
from sentinelgate.services.router import ProviderRouter


@pytest.fixture
def pipeline_app():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_f:
        db_path = db_f.name

    test_config = {
        "server": {
            "host": "127.0.0.1",
            "port": 8000,
            "admin_token": "admin-pipeline-token",
            "cors_origins": [],
        },
        "cache": {
            "enabled": True,
            "similarity_threshold": 0.90,
            "ttl_seconds": 3600,
            "db_path": db_path,
            "exact_match_fallback": True,
        },
        "guardrails": {
            "pii_masking": True,
            "block_secrets": True,
            "prompt_injection_block": True,
            "context_compaction": True,
        },
        "budget": {"enabled": True, "daily_limit_usd": 100.0},
        "evals": {"enabled": True, "sample_rate": 0.0},
        "providers": {
            "openai": {
                "type": "openai",
                "base_url": "https://api.openai.com/v1",
                "api_keys": ["sk-mock-key-1"],
                "models": {
                    "gpt-4o-mini": {
                        "input_cost_per_m": 0.15,
                        "output_cost_per_m": 0.60,
                        "fallbacks": ["groq/llama-3.1-8b-instant"],
                    }
                },
            },
            "groq": {
                "type": "openai_compatible",
                "base_url": "https://api.groq.com/openai/v1",
                "api_keys": ["groq-mock-key-1"],
                "models": {
                    "llama-3.1-8b-instant": {
                        "input_cost_per_m": 0.05,
                        "output_cost_per_m": 0.08,
                        "fallbacks": [],
                    }
                },
            },
            "anthropic": {
                "type": "anthropic",
                "base_url": "https://api.anthropic.com/v1",
                "api_keys": ["ant-mock-key-1"],
                "models": {
                    "claude-3-5-sonnet-20241022": {
                        "input_cost_per_m": 0.80,
                        "output_cost_per_m": 4.0,
                        "fallbacks": ["groq/llama-3.1-8b-instant"],
                    }
                },
            },
        },
    }

    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as cfg_f:
        yaml.safe_dump(test_config, cfg_f)
        cfg_path = cfg_f.name

    app_instance = create_app(config_path=cfg_path, db_path=db_path)
    client = TestClient(app_instance, headers={"Authorization": "Bearer admin-pipeline-token"})

    yield client, app_instance

    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except OSError:
            pass
    if os.path.exists(cfg_path):
        try:
            os.remove(cfg_path)
        except OSError:
            pass


# =============================================================================
# 1. ACCEPTANCE: 4-CELL TEST MATRIX FOR TOOL CALL FIREWALL
#    {openai, anthropic} x {stream, non-stream} x {safe, malicious}
# =============================================================================


@pytest.mark.parametrize("is_malicious", [False, True])
@respx.mock
def test_tool_firewall_cell1_openai_non_stream(pipeline_app, is_malicious: bool):
    """Cell 1: OpenAI Non-Streaming x {Safe, Malicious}."""
    client, app = pipeline_app
    app.state.router.client = None

    tool_args = {"command": "rm -rf /"} if is_malicious else {"command": "git status"}
    openai_mock_response = {
        "id": "chatcmpl-cell-1",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_cell_1",
                            "type": "function",
                            "function": {
                                "name": "execute_bash",
                                "arguments": json.dumps(tool_args),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 15, "completion_tokens": 10, "total_tokens": 25},
    }

    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=openai_mock_response)
    )

    req_payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "Run the task"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "execute_bash",
                    "description": "Run shell command",
                    "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
                },
            }
        ],
        "stream": False,
    }

    resp = client.post("/v1/chat/completions", json=req_payload)
    assert resp.status_code == 200

    data = resp.json()
    if is_malicious:
        assert resp.headers.get("X-Agent-Firewall-Blocked") == "true"
        assert "blocked tool execution" in data["choices"][0]["message"]["content"].lower()
        assert data["choices"][0]["message"].get("tool_calls") is None
    else:
        assert resp.headers.get("X-Agent-Firewall-Blocked") == "false"
        assert data["choices"][0]["message"]["tool_calls"] is not None
        assert data["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "execute_bash"


@pytest.mark.parametrize("is_malicious", [False, True])
@respx.mock
def test_tool_firewall_cell2_openai_stream(pipeline_app, is_malicious: bool):
    """Cell 2: OpenAI Streaming x {Safe, Malicious}."""
    client, app = pipeline_app
    app.state.router.client = None

    tool_args_str = (
        json.dumps({"command": "rm -rf /"})
        if is_malicious
        else json.dumps({"command": "git status"})
    )

    stream_lines = [
        f"data: {json.dumps({'id': 'chatcmpl-stream-2', 'object': 'chat.completion.chunk', 'created': 1700000000, 'model': 'gpt-4o-mini', 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'tool_calls': [{'index': 0, 'id': 'call_stream_2', 'type': 'function', 'function': {'name': 'execute_bash', 'arguments': ''}}]}, 'finish_reason': None}]})}\n\n",
        f"data: {json.dumps({'id': 'chatcmpl-stream-2', 'object': 'chat.completion.chunk', 'created': 1700000000, 'model': 'gpt-4o-mini', 'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': tool_args_str}}]}, 'finish_reason': None}]})}\n\n",
        f"data: {json.dumps({'id': 'chatcmpl-stream-2', 'object': 'chat.completion.chunk', 'created': 1700000000, 'model': 'gpt-4o-mini', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]})}\n\n",
        "data: [DONE]\n\n",
    ]

    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, text="".join(stream_lines), headers={"content-type": "text/event-stream"}
        )
    )

    req_payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "Run shell task"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "execute_bash",
                    "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
                },
            }
        ],
        "stream": True,
    }

    resp = client.post("/v1/chat/completions", json=req_payload)
    assert resp.status_code == 200
    body = resp.text

    if is_malicious:
        # Tool call MUST be suppressed and replaced with refusal
        assert "blocked tool execution" in body.lower()
        assert "rm -rf" not in body
    else:
        assert "execute_bash" in body
        assert "git status" in body


@pytest.mark.parametrize("is_malicious", [False, True])
@respx.mock
def test_tool_firewall_cell3_anthropic_non_stream(pipeline_app, is_malicious: bool):
    """Cell 3: Anthropic Non-Streaming x {Safe, Malicious}."""
    client, app = pipeline_app
    app.state.router.client = None

    tool_args = {"command": "rm -rf /"} if is_malicious else {"command": "git status"}
    anthropic_mock_response = {
        "id": "msg_cell_3",
        "type": "message",
        "role": "assistant",
        "model": "claude-3-5-sonnet-20241022",
        "content": [
            {
                "type": "tool_use",
                "id": "call_cell_3",
                "name": "execute_bash",
                "input": tool_args,
            }
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 8},
    }

    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json=anthropic_mock_response)
    )

    anthropic_payload = {
        "model": "claude-3-5-sonnet-20241022",
        "messages": [{"role": "user", "content": "Execute terminal task"}],
        "tools": [
            {
                "name": "execute_bash",
                "description": "Execute bash",
                "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}},
            }
        ],
        "stream": False,
    }

    resp = client.post("/v1/messages", json=anthropic_payload)
    assert resp.status_code == 200

    data = resp.json()
    assert data["type"] == "message"
    assert "usage" in data
    assert data["usage"]["input_tokens"] > 0

    if is_malicious:
        assert resp.headers.get("X-Agent-Firewall-Blocked") == "true"
        # Content block should be text refusal, NOT tool_use
        assert data["content"][0]["type"] == "text"
        assert "blocked tool execution" in data["content"][0]["text"].lower()
        assert data["stop_reason"] == "end_turn"
    else:
        assert resp.headers.get("X-Agent-Firewall-Blocked") == "false"
        assert data["content"][0]["type"] == "tool_use"
        assert data["content"][0]["name"] == "execute_bash"
        assert data["stop_reason"] == "tool_use"


@pytest.mark.parametrize("is_malicious", [False, True])
@respx.mock
def test_tool_firewall_cell4_anthropic_stream(pipeline_app, is_malicious: bool):
    """Cell 4: Anthropic Streaming x {Safe, Malicious}."""
    client, app = pipeline_app
    app.state.router.client = None

    tool_args_str = (
        json.dumps({"command": "rm -rf /"})
        if is_malicious
        else json.dumps({"command": "git status"})
    )

    stream_lines = [
        f"data: {json.dumps({'type': 'message_start', 'message': {'id': 'msg_stream_4', 'type': 'message', 'role': 'assistant', 'model': 'claude-3-5-sonnet-20241022', 'content': [], 'stop_reason': None, 'usage': {'input_tokens': 10, 'output_tokens': 0}}})}\n\n",
        f"data: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'tool_use', 'id': 'call_stream_4', 'name': 'execute_bash', 'input': {}}})}\n\n",
        f"data: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': tool_args_str}})}\n\n",
        f"data: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n",
        f"data: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 15}})}\n\n",
        "data: [DONE]\n\n",
    ]

    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, text="".join(stream_lines), headers={"content-type": "text/event-stream"}
        )
    )

    anthropic_payload = {
        "model": "claude-3-5-sonnet-20241022",
        "messages": [{"role": "user", "content": "Execute stream command"}],
        "tools": [
            {
                "name": "execute_bash",
                "description": "Execute bash",
                "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}},
            }
        ],
        "stream": True,
    }

    resp = client.post("/v1/messages", json=anthropic_payload)
    assert resp.status_code == 200
    body = resp.text

    # Verify Anthropic SSE event sequence
    assert "event: message_start" in body
    assert "event: ping" in body
    assert "event: message_delta" in body
    assert "event: message_stop" in body

    if is_malicious:
        assert "blocked tool execution" in body.lower()
        assert "rm -rf" not in body
    else:
        assert "event: content_block_start" in body
        assert "tool_use" in body
        assert "git status" in body


# =============================================================================
# 2. ACCEPTANCE: TENANT CACHE ISOLATION TEST
# =============================================================================


@respx.mock
def test_tenant_cache_isolation(pipeline_app):
    """Assert Tenant A cannot read Tenant B's cached response."""
    client, app = pipeline_app
    app.state.router.client = None

    # Create Virtual Key for Tenant A
    res_a = client.post("/v1/keys", json={"name": "tenant-a-key", "rate_limit_rpm": 100})
    vk_a = res_a.json()["key"]

    # Create Virtual Key for Tenant B
    res_b = client.post("/v1/keys", json={"name": "tenant-b-key", "rate_limit_rpm": 100})
    vk_b = res_b.json()["key"]

    prompt_text = "Explain the safety invariants of Raft consensus."
    req_body = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt_text}],
        "stream": False,
    }

    # Upstream returns distinct response for Tenant A
    resp_a_mock = {
        "id": "chatcmpl-t-a",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Raft response for Tenant A"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
    }
    # Upstream returns distinct response for Tenant B
    resp_b_mock = {
        "id": "chatcmpl-t-b",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Raft response for Tenant B"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
    }

    mock_route = respx.post("https://api.openai.com/v1/chat/completions")
    mock_route.side_effect = [
        httpx.Response(200, json=resp_a_mock),
        httpx.Response(200, json=resp_b_mock),
    ]

    # Tenant A request 1 -> Cache MISS, calls upstream
    r1_a = client.post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {vk_a}"}, json=req_body
    )
    assert r1_a.status_code == 200
    assert r1_a.headers["X-Cache"] == "MISS"
    assert r1_a.json()["choices"][0]["message"]["content"] == "Raft response for Tenant A"

    # Tenant B request 1 with EXACT SAME PROMPT -> Must be Cache MISS (Tenant A cache entry isolated)
    r1_b = client.post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {vk_b}"}, json=req_body
    )
    assert r1_b.status_code == 200
    assert r1_b.headers["X-Cache"] == "MISS"
    assert r1_b.json()["choices"][0]["message"]["content"] == "Raft response for Tenant B"

    # Tenant A request 2 -> Cache HIT with Tenant A's cached response
    r2_a = client.post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {vk_a}"}, json=req_body
    )
    assert r2_a.status_code == 200
    assert r2_a.headers["X-Cache"] == "HIT"
    assert r2_a.json()["choices"][0]["message"]["content"] == "Raft response for Tenant A"

    # Tenant B request 2 -> Cache HIT with Tenant B's cached response
    r2_b = client.post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {vk_b}"}, json=req_body
    )
    assert r2_b.status_code == 200
    assert r2_b.headers["X-Cache"] == "HIT"
    assert r2_b.json()["choices"][0]["message"]["content"] == "Raft response for Tenant B"


# =============================================================================
# 3. ACCEPTANCE: RESPONSES WITH TOOL CALLS NEVER CACHED
# =============================================================================


@respx.mock
def test_tool_call_responses_never_cached(pipeline_app):
    """Responses containing tool calls must NEVER be cached (stateful actions)."""
    client, app = pipeline_app
    app.state.router.client = None

    tool_resp = {
        "id": "chatcmpl-tool-never-cache",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_stat_1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"path": "file.txt"}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }

    route = respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=tool_resp)
    )

    req_body = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "Check the file status"}],
        "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {}}}],
        "stream": False,
    }

    # Request 1
    r1 = client.post("/v1/chat/completions", json=req_body)
    assert r1.status_code == 200
    assert r1.headers["X-Cache"] == "MISS"

    # Request 2 (Identical payload) -> Must still be MISS, never cached!
    r2 = client.post("/v1/chat/completions", json=req_body)
    assert r2.status_code == 200
    assert r2.headers["X-Cache"] == "MISS"
    assert route.call_count == 2


# =============================================================================
# 4. ACCEPTANCE: EMBEDDING FALLBACK & HEALTH STATUS
# =============================================================================


def test_cache_degrades_gracefully_to_exact_match(tmp_path):
    """If FastEmbed/ONNX fails to initialize, degrade cleanly to exact-match only."""
    from sentinelgate.core.database import init_db

    db_file = str(tmp_path / "cache_offline.db")
    init_db(db_file)

    with patch("fastembed.TextEmbedding", side_effect=ImportError("ONNX offline / air-gapped")):
        cache = SemanticCache(db_path=db_file, exact_match_fallback=True)
        assert cache.is_semantic_enabled is False
        assert cache.model_status == "degraded_exact_match_only"

        # Storing and exact lookup still works
        messages = [{"role": "user", "content": "What is SQLite WAL?"}]
        response = {
            "choices": [{"message": {"role": "assistant", "content": "Write-Ahead Logging"}}]
        }

        cache.store(messages=messages, model="gpt-4o-mini", response=response)

        # Exact match succeeds
        result = cache.lookup(messages=messages, model="gpt-4o-mini")
        assert result is not None
        assert result[1] == 1.0
        assert result[0]["choices"][0]["message"]["content"] == "Write-Ahead Logging"

        # Semantic/paraphrased match returns None (does not crash or hang)
        paraphrased = [{"role": "user", "content": "Tell me about SQLite WAL mode"}]
        assert cache.lookup(messages=paraphrased, model="gpt-4o-mini") is None


def test_health_endpoint_exposes_model_status(pipeline_app):
    """Verify /health exposes embedding_model_status without auth requirement."""
    client, app = pipeline_app
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert "embedding_model_status" in data
    assert data["embedding_model_status"] in ("ready", "degraded_exact_match_only")


# =============================================================================
# 5. ACCEPTANCE: SPECULATIVE HEDGING CANCELS LOSER TASK
# =============================================================================


@pytest.mark.asyncio
async def test_speculative_hedging_cancels_loser_task():
    """Verify speculative latency hedging explicitly calls task.cancel() on the loser."""
    router = ProviderRouter()

    primary_cancelled = False
    secondary_cancelled = False

    async def mock_primary(payload, stream=False):
        nonlocal primary_cancelled
        try:
            await asyncio.sleep(2.0)
            return {"primary": True}, "openai", "gpt-4o-mini"
        except asyncio.CancelledError:
            primary_cancelled = True
            raise

    async def mock_secondary(payload, stream=False):
        nonlocal secondary_cancelled
        try:
            await asyncio.sleep(0.02)
            return {"secondary": True}, "groq", "llama-3.1-8b-instant"
        except asyncio.CancelledError:
            secondary_cancelled = True
            raise

    call_count = 0

    async def mock_exec(payload, stream=False):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return await mock_primary(payload, stream)
        else:
            return await mock_secondary(payload, stream)

    with patch.object(router, "execute_request", side_effect=mock_exec):
        with patch.object(
            router,
            "resolve_model",
            return_value=("openai", "gpt-4o-mini", ["groq/llama-3.1-8b-instant"]),
        ):
            res, prov, mod, meta = await router.execute_hedged_request(
                request_data={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": "hedge test"}],
                },
                hedge_delay_sec=0.01,
            )

            assert meta["hedged_triggered"] is True
            assert meta["winner"] == "hedge"
            assert "hedged" in prov

            # Allow loop tick for cancellation propagation
            await asyncio.sleep(0.05)
            assert primary_cancelled is True
            assert secondary_cancelled is False
