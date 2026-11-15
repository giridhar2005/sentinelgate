from __future__ import annotations

import httpx
import respx


@respx.mock
def test_dual_bucket_rate_limiter_tpm_exceeded(test_app):
    client, app = test_app
    app.state.router.client = None

    # Create virtual key with 1000 RPM but only 50 TPM limit
    create_res = client.post(
        "/v1/keys",
        json={"name": "tpm-test-key", "rate_limit_rpm": 1000, "rate_limit_tpm": 50},
    )
    assert create_res.status_code == 200
    data = create_res.json()
    vk = data["key"]
    assert data["rate_limit_tpm"] == 50

    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-tpm-1",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "TPM OK"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            },
        )
    )

    headers = {"Authorization": f"Bearer {vk}", "X-No-Cache": "true"}
    req_body = {
        "model": "gpt-4o-mini",
        # 10 words prompt ~= 10 tokens
        "messages": [{"role": "user", "content": "hello world test prompt"}],
    }

    # Request 1 -> 200 OK, returns rate limit headers
    r1 = client.post("/v1/chat/completions", headers=headers, json=req_body)
    assert r1.status_code == 200
    assert "x-ratelimit-limit-requests" in r1.headers
    assert "x-ratelimit-remaining-requests" in r1.headers
    assert "x-ratelimit-limit-tokens" in r1.headers
    assert "x-ratelimit-remaining-tokens" in r1.headers
    assert r1.headers["x-ratelimit-limit-tokens"] == "50"

    # Now attempt request with large prompt (~80 tokens) that exceeds remaining TPM limit (50)
    large_prompt = "word " * 80
    r_exceed = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": large_prompt}]},
    )
    assert r_exceed.status_code == 429
    assert r_exceed.json()["error"]["code"] == "rate_limit_exceeded"
    assert "TPM limit" in r_exceed.json()["error"]["message"]
    assert "x-ratelimit-limit-tokens" in r_exceed.headers
