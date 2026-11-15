from __future__ import annotations

import httpx
import respx


def test_health_check(test_app):
    client, _ = test_app
    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "healthy"
    assert data["service"] == "SentinelGate"


def test_models_endpoint(test_app):
    client, _ = test_app
    res = client.get("/v1/models")
    assert res.status_code == 200
    model_ids = [m["id"] for m in res.json()["data"]]
    assert "gpt-4o-mini" in model_ids
    assert "llama-3.1-8b-instant" in model_ids


@respx.mock
def test_non_streaming_chat_completion_with_headers(test_app):
    client, app = test_app
    app.state.router.client = None

    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-test-1",
                "object": "chat.completion",
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Hello there!"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 12, "total_tokens": 22},
            },
        )
    )

    resp = client.post(
        "/v1/chat/completions",
        headers={"X-No-Cache": "true"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Say hello"}]},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["choices"][0]["message"]["content"] == "Hello there!"

    # Verify required headers
    assert "X-Request-ID" in resp.headers
    assert resp.headers.get("X-Cache") == "MISS"
    assert resp.headers.get("X-Provider-Used") == "openai"
    assert "X-Latency-Ms" in resp.headers


@respx.mock
def test_streaming_sse_completion(test_app):
    client, app = test_app
    app.state.router.client = None

    sse_payload = (
        'data: {"id":"chatcmpl-stream","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"Chunk1"}}]}\n\n'
        'data: {"id":"chatcmpl-stream","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":" Chunk2"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, text=sse_payload, headers={"content-type": "text/event-stream"}
        )
    )

    resp = client.post(
        "/v1/chat/completions",
        headers={"X-No-Cache": "true"},
        json={
            "model": "gpt-4o-mini",
            "stream": True,
            "messages": [{"role": "user", "content": "Stream"}],
        },
    )

    assert resp.status_code == 200
    assert resp.headers.get("content-type").startswith("text/event-stream")
    assert "Chunk1" in resp.text
    assert "Chunk2" in resp.text
    assert resp.headers.get("X-Cache") == "MISS"
    assert resp.headers.get("X-Provider-Used") == "openai"
