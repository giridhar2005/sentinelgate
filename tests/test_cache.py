from __future__ import annotations

import httpx
import respx


@respx.mock
def test_semantic_cache_hit_and_zero_cost(test_app):
    client, app = test_app
    app.state.router.client = None

    upstream_mock = respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-cache-1",
                "object": "chat.completion",
                "model": "gpt-4o-mini",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "Superposition lets quantum bits be 0 and 1 simultaneously."},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 15, "total_tokens": 25},
            },
        )
    )

    # 1. First request: Cache Miss -> calls upstream
    r1 = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Explain quantum superposition clearly"}]},
    )
    assert r1.status_code == 200
    assert r1.headers.get("X-Cache") == "MISS"
    assert upstream_mock.call_count == 1

    # 2. Second request: Semantically similar query -> Cache Hit ($0.00 cost, upstream NOT called again)
    r2 = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Explain quantum superposition clearly"}]},
    )
    assert r2.status_code == 200
    assert r2.headers.get("X-Cache") == "HIT"
    assert upstream_mock.call_count == 1  # Unchanged!
    assert "Superposition" in r2.json()["choices"][0]["message"]["content"]


@respx.mock
def test_cache_bypass_headers(test_app):
    client, app = test_app
    app.state.router.client = None

    upstream_mock = respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-bypass",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "Fresh response"}}],
            },
        )
    )

    # Seed cache
    client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Hello cache bypass"}]},
    )
    assert upstream_mock.call_count == 1

    # Request with X-No-Cache: true -> must bypass cache
    r_bypass1 = client.post(
        "/v1/chat/completions",
        headers={"X-No-Cache": "true"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Hello cache bypass"}]},
    )
    assert r_bypass1.status_code == 200
    assert r_bypass1.headers.get("X-Cache") == "MISS"
    assert upstream_mock.call_count == 2

    # Request with Cache-Control: no-cache -> must bypass cache
    r_bypass2 = client.post(
        "/v1/chat/completions",
        headers={"Cache-Control": "no-cache"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Hello cache bypass"}]},
    )
    assert r_bypass2.status_code == 200
    assert r_bypass2.headers.get("X-Cache") == "MISS"
    assert upstream_mock.call_count == 3


def test_cache_ttl_expiration(test_app):
    client, app = test_app

    # Store with TTL of -10 seconds (already expired)
    app.state.cache.store(
        messages=[{"role": "user", "content": "Expiring query"}],
        model="gpt-4o-mini",
        response={"choices": [{"message": {"content": "Old data"}}]},
        ttl_seconds=-10,
    )

    lookup_result = app.state.cache.lookup(
        messages=[{"role": "user", "content": "Expiring query"}],
        model="gpt-4o-mini",
    )
    assert lookup_result is None
