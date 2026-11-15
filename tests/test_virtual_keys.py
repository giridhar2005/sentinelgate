from __future__ import annotations

import httpx
import respx


def test_create_and_list_virtual_keys(test_app):
    client, _ = test_app

    # Create virtual key
    resp = client.post(
        "/v1/keys",
        json={"name": "analytics-worker", "daily_budget_usd": 25.0, "rate_limit_rpm": 120},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "key" in data
    assert data["key"].startswith("sg-live-anal")
    assert data["name"] == "analytics-worker"
    assert data["rate_limit_rpm"] == 120

    # List virtual keys
    list_resp = client.get("/v1/keys")
    assert list_resp.status_code == 200
    keys = list_resp.json()["keys"]
    assert any(k["name"] == "analytics-worker" for k in keys)


@respx.mock
def test_virtual_key_rate_limiting(test_app):
    client, app = test_app
    app.state.router.client = None

    # Create key with 2 RPM limit
    k_res = client.post(
        "/v1/keys",
        json={"name": "test-ratelimit", "daily_budget_usd": 10.0, "rate_limit_rpm": 2},
    )
    assert k_res.status_code == 200
    vk = k_res.json()["key"]

    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-vk",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "VK OK"}}],
            },
        )
    )

    headers = {"Authorization": f"Bearer {vk}", "X-No-Cache": "true"}
    req_body = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "ping"}]}

    # Request 1 -> 200 OK
    r1 = client.post("/v1/chat/completions", headers=headers, json=req_body)
    assert r1.status_code == 200

    # Request 2 -> 200 OK (2 rpm limit reached)
    r2 = client.post("/v1/chat/completions", headers=headers, json=req_body)
    assert r2.status_code == 200

    # Request 3 -> 429 Rate Limit Exceeded
    r3 = client.post("/v1/chat/completions", headers=headers, json=req_body)
    assert r3.status_code == 429
    assert r3.json()["error"]["code"] == "rate_limit_exceeded"
    assert "Rate limit exceeded" in r3.json()["error"]["message"]


def test_invalid_virtual_key_rejection(test_app):
    client, _ = test_app

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sg-live-invalid-key-999"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Hello"}]},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_api_key"


@respx.mock
def test_budget_circuit_breaker(test_app):
    client, app = test_app
    app.state.router.client = None

    # Set tiny daily budget threshold: $0.0001
    app.state.budget.daily_limit_usd = 0.0001

    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-budget-1",
                "object": "chat.completion",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "Costly generation"}}
                ],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 1000, "total_tokens": 2000},
            },
        )
    )

    # First request consumes budget
    r1 = client.post(
        "/v1/chat/completions",
        headers={"X-No-Cache": "true"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Spend money"}]},
    )
    assert r1.status_code == 200

    # Second request hits daily budget circuit breaker
    r2 = client.post(
        "/v1/chat/completions",
        headers={"X-No-Cache": "true"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Spend more"}]},
    )
    assert r2.status_code == 429
    assert r2.json()["error"]["code"] == "budget_limit_exceeded"
    assert "Daily Budget Limit Exceeded" in r2.json()["error"]["message"]
