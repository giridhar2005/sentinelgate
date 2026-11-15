from __future__ import annotations

import httpx
import pytest
import respx


@pytest.mark.asyncio
async def test_speculative_hedging_primary_wins(test_app):
    client, app = test_app
    gateway_router = app.state.router
    # Force sandbox mode for deterministic testing
    gateway_router.demo_mode = True

    req_data = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "fast speculative prompt"}],
    }

    res, provider, model, hedge_meta = await gateway_router.execute_hedged_request(
        req_data, hedge_delay_sec=0.5
    )
    assert res is not None
    assert hedge_meta["winner"] == "primary"
    assert hedge_meta["hedged_triggered"] is False


@pytest.mark.asyncio
async def test_speculative_hedging_race_trigger(test_app):
    client, app = test_app
    gateway_router = app.state.router
    gateway_router.demo_mode = True

    req_data = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "speculative race prompt"}],
    }

    # Setting hedge_delay_sec to 0.001 forces hedge trigger
    res, provider, model, hedge_meta = await gateway_router.execute_hedged_request(
        req_data, hedge_delay_sec=0.001
    )
    assert res is not None
    assert hedge_meta["hedged_triggered"] is True
    assert hedge_meta["winner"] in ("primary", "hedge")


@respx.mock
def test_speculative_hedging_http_endpoint_headers(test_app):
    client, app = test_app
    app.state.router.client = None
    app.state.router.demo_mode = False

    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-hedge",
                "object": "chat.completion",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "Hedge race OK"}}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )
    )

    req_body = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "Hedged request test"}],
        "hedge": True,
    }

    r = client.post("/v1/chat/completions", headers={"X-No-Cache": "true"}, json=req_body)
    assert r.status_code == 200
    assert r.headers.get("x-hedged-request") == "true"
    assert "x-hedge-winner" in r.headers
