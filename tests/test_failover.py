from __future__ import annotations

import httpx
import respx


@respx.mock
def test_provider_key_rotation(test_app):
    client, app = test_app
    app.state.router.client = None

    openai_mock = respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-rot-1",
                "object": "chat.completion",
                "model": "gpt-4o-mini",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "Rotated response"}}
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
            },
        )
    )

    # 1st request -> uses key-alpha
    r1 = client.post(
        "/v1/chat/completions",
        headers={"X-No-Cache": "true"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Rot 1"}]},
    )
    assert r1.status_code == 200
    assert openai_mock.calls[0].request.headers["authorization"] == "Bearer key-alpha"

    # 2nd request -> rotates to key-beta
    r2 = client.post(
        "/v1/chat/completions",
        headers={"X-No-Cache": "true"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Rot 2"}]},
    )
    assert r2.status_code == 200
    assert openai_mock.calls[1].request.headers["authorization"] == "Bearer key-beta"


@respx.mock
def test_429_automatic_failover(test_app):
    client, app = test_app
    app.state.router.client = None

    # Primary returns 429
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(429, json={"error": "Rate limit exceeded"})
    )

    # Fallback (Groq) succeeds
    groq_mock = respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-fallback-429",
                "object": "chat.completion",
                "model": "llama-3.1-8b-instant",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "Groq 429 fallback"}}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            },
        )
    )

    resp = client.post(
        "/v1/chat/completions",
        headers={"X-No-Cache": "true"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "Failover 429 test"}],
        },
    )

    assert resp.status_code == 200
    assert groq_mock.called
    assert resp.headers.get("X-Provider-Used") == "groq"
    assert "Groq 429 fallback" in resp.json()["choices"][0]["message"]["content"]


@respx.mock
def test_503_automatic_failover(test_app):
    client, app = test_app
    app.state.router.client = None

    # Primary returns 503
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(503, text="Service Unavailable")
    )

    # Fallback (Groq) succeeds
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-fallback-503",
                "object": "chat.completion",
                "model": "llama-3.1-8b-instant",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "503 recovered"}}
                ],
            },
        )
    )

    resp = client.post(
        "/v1/chat/completions",
        headers={"X-No-Cache": "true"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "Failover 503 test"}],
        },
    )

    assert resp.status_code == 200
    assert resp.headers.get("X-Provider-Used") == "groq"
    assert "503 recovered" in resp.json()["choices"][0]["message"]["content"]


@respx.mock
def test_circuit_breaker_cooldown_and_health(test_app):
    client, app = test_app
    app.state.router.client = None

    # Primary returns 500 on all attempts
    openai_mock = respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(500, json={"error": "Internal Server Error"})
    )

    # Fallback returns 200
    groq_mock = respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-groq-cb",
                "object": "chat.completion",
                "model": "llama-3.1-8b-instant",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "Groq CB responder"}}
                ],
            },
        )
    )

    # Request 1: Tries OpenAI keys, trips circuit breaker, then succeeds via Groq
    r1 = client.post(
        "/v1/chat/completions",
        headers={"X-No-Cache": "true"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "CB Request 1"}]},
    )
    assert r1.status_code == 200
    assert openai_mock.call_count == 2
    assert groq_mock.call_count == 1

    # Check /v1/providers/health: OpenAI is in cooldown
    h_resp = client.get("/v1/providers/health")
    assert h_resp.status_code == 200
    providers_st = h_resp.json()["providers"]
    assert providers_st["openai"]["status"] == "cooldown"
    assert providers_st["openai"]["cooldown_remaining_sec"] > 0
    assert providers_st["groq"]["status"] == "healthy"

    # Request 2: Because OpenAI is in active cooldown, router skips OpenAI immediately!
    r2 = client.post(
        "/v1/chat/completions",
        headers={"X-No-Cache": "true"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "CB Request 2"}]},
    )
    assert r2.status_code == 200
    # OpenAI call count must strictly remain 2 (zero overhead / skipped)
    assert openai_mock.call_count == 2
    assert groq_mock.call_count == 2
