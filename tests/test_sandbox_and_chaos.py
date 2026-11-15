from __future__ import annotations


def test_sandbox_mode_chat_completion(test_app, monkeypatch):
    client, app = test_app
    monkeypatch.setenv("DEMO_MODE", "true")

    # Non-streaming prompt in sandbox mode
    resp = client.post(
        "/v1/chat/completions",
        headers={"X-No-Cache": "true"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "Explain raft consensus in two sentences"}],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "choices" in data
    assert len(data["choices"]) > 0
    content = data["choices"][0]["message"]["content"]
    assert "Raft" in content
    assert data["usage"]["prompt_tokens"] > 0
    assert data["usage"]["completion_tokens"] > 0
    assert resp.headers.get("X-Cache") == "MISS"
    assert resp.headers.get("X-Provider-Used") == "openai"


def test_sandbox_mode_streaming(test_app, monkeypatch):
    client, app = test_app
    monkeypatch.setenv("DEMO_MODE", "true")

    resp = client.post(
        "/v1/chat/completions",
        headers={"X-No-Cache": "true"},
        json={
            "model": "gpt-4o-mini",
            "stream": True,
            "messages": [{"role": "user", "content": "Explain quicksort"}],
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("content-type").startswith("text/event-stream")
    assert "data: " in resp.text
    assert "[DONE]" in resp.text
    assert "QuickSort" in resp.text


def test_chaos_toggle_and_automatic_failover(test_app, monkeypatch):
    client, app = test_app
    monkeypatch.setenv("DEMO_MODE", "true")

    # 1. Verify chaos status endpoint
    status_resp = client.get("/api/chaos")
    assert status_resp.status_code == 200
    assert status_resp.json()["enabled"] is False

    # 2. Enable chaos targeting openai
    toggle_resp = client.post(
        "/api/chaos", json={"enabled": True, "target_provider": "openai", "status_code": 429}
    )
    assert toggle_resp.status_code == 200
    assert toggle_resp.json()["enabled"] is True
    assert toggle_resp.json()["target_provider"] == "openai"

    # 3. Send request targeting gpt-4o-mini (whose primary provider is openai)
    req_resp = client.post(
        "/v1/chat/completions",
        headers={"X-No-Cache": "true"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "Chaos failover simulation prompt"}],
        },
    )
    assert req_resp.status_code == 200
    # Because openai had 429 chaos injected, it failed over to groq!
    assert req_resp.headers.get("X-Provider-Used") == "groq"

    # 4. Turn chaos off
    disable_resp = client.post("/api/chaos", json={"enabled": False})
    assert disable_resp.status_code == 200
    assert disable_resp.json()["enabled"] is False

    # Request now uses openai normally
    normal_resp = client.post(
        "/v1/chat/completions",
        headers={"X-No-Cache": "true"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "Back to normal prompt"}],
        },
    )
    assert normal_resp.status_code == 200
    assert normal_resp.headers.get("X-Provider-Used") == "openai"


def test_shadow_replay_cost_estimator(test_app, monkeypatch):
    client, app = test_app
    monkeypatch.setenv("DEMO_MODE", "true")

    replay_payload = {
        "prompt": "Explain the raft consensus algorithm in two sentences.",
        "original_model": "gpt-4o",
        "candidate_model": "groq/llama-3.1-8b-instant",
        "original_latency_ms": 520.0,
        "original_cost_usd": 0.002500,
        "original_response": "Raft is a leader-based consensus algorithm...",
    }

    resp = client.post("/api/replay", json=replay_payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["candidate_model"] == "groq/llama-3.1-8b-instant"
    assert data["candidate_provider"] == "groq"
    assert "candidate_response" in data
    assert data["candidate_latency_ms"] > 0
    assert data["latency_diff_ms"] < 0  # Faster than 520ms!
    assert data["projected_monthly_savings_100k"] > 0.0
    assert data["savings_percent"] > 50.0  # Llama-3.1-8b is drastically cheaper than GPT-4o
