from __future__ import annotations

import httpx
import respx


def test_prometheus_metrics_endpoint(test_app):
    client, app = test_app
    app.state.router.client = None

    # Fetch initial metrics
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    body = resp.text

    assert "sentinelgate_up 1" in body
    assert "# HELP sentinelgate_requests_total" in body
    assert "# TYPE sentinelgate_requests_total counter"
    assert "sentinelgate_tokens_total" in body
    assert "sentinelgate_cache_lookups_total" in body
    assert "sentinelgate_request_duration_seconds_bucket" in body
    assert "sentinelgate_hedged_requests_total" in body


@respx.mock
def test_prometheus_metrics_increment_on_traffic(test_app):
    client, app = test_app
    app.state.router.client = None

    # Mock OpenAI
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-prom",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Prometheus test response"},
                    }
                ],
                "usage": {"prompt_tokens": 15, "completion_tokens": 8, "total_tokens": 23},
            },
        )
    )

    req_body = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "Generate metrics test payload"}],
    }

    # Send a request with cache bypass
    r = client.post("/v1/chat/completions", headers={"X-No-Cache": "true"}, json=req_body)
    assert r.status_code == 200

    # Scrape metrics again
    m_resp = client.get("/metrics")
    assert m_resp.status_code == 200
    m_text = m_resp.text

    assert (
        'sentinelgate_requests_total{model="gpt-4o-mini",provider="openai",status="UPSTREAM"} 1'
        in m_text
    )
    assert 'sentinelgate_tokens_total{type="prompt"} 15' in m_text
    assert 'sentinelgate_tokens_total{type="completion"} 8' in m_text
    assert 'sentinelgate_cache_lookups_total{result="miss"} 1' in m_text
