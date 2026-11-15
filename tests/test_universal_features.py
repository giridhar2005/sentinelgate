from __future__ import annotations

import pytest

from app.services.discovery import ProviderDiscovery, format_startup_banner
from app.services.guardrails import SecretRedactor


def test_secret_redactor_unit():
    raw_prompt = (
        "Here is my database url: postgresql://admin:secretPass123@db.prod.internal:5432/production "
        "and my OpenAI key sk-proj-1234567890abcdef1234567890 along with AWS AKIAIOSFODNN7EXAMPLE. "
        "Also my email is dev@company.com."
    )

    messages = [{"role": "user", "content": raw_prompt}]
    sanitized, mapping, count = SecretRedactor.redact_messages(messages)

    assert count >= 4
    content = sanitized[0]["content"]
    assert "postgresql://" not in content
    assert "secretPass123" not in content
    assert "sk-proj-" not in content
    assert "AKIAIOSFODNN7EXAMPLE" not in content
    assert "__SG_SECRET_" in content

    # Test restoration in response
    simulated_resp = {
        "id": "chatcmpl-test",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": f"Verified config with {list(mapping.keys())[0]} and {list(mapping.keys())[1]}.",
                }
            }
        ],
    }
    restored = SecretRedactor.restore_in_response(simulated_resp, mapping)
    restored_text = restored["choices"][0]["message"]["content"]
    assert "__SG_SECRET_" not in restored_text
    assert (
        ("postgresql://" in restored_text)
        or ("sk-proj-" in restored_text)
        or ("AKIA" in restored_text)
    )


def test_reversible_secret_redaction_endpoint(test_app, monkeypatch):
    client, app = test_app
    monkeypatch.setenv("DEMO_MODE", "true")

    # Send prompt with secret in redact mode
    prompt_with_secret = (
        "Connect to mongodb://cluster0.example.net:27017/admin with sk-proj-9999999999999999999999"
    )

    resp = client.post(
        "/v1/chat/completions",
        headers={"X-Secret-Action": "redact"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt_with_secret}],
        },
    )

    assert resp.status_code == 200
    assert "X-Secrets-Scrubbed" in resp.headers
    assert int(resp.headers["X-Secrets-Scrubbed"]) >= 1

    # Verify metrics logged scrubbed event
    metrics_resp = client.get("/api/metrics")
    assert metrics_resp.status_code == 200
    assert metrics_resp.json().get("secrets_scrubbed", 0) >= 1


def test_model_auto_routing_simple_vs_complex(test_app, monkeypatch):
    client, app = test_app
    monkeypatch.setenv("DEMO_MODE", "true")

    # 1. Simple routine prompt -> routes to cheap utility model
    simple_resp = client.post(
        "/v1/chat/completions",
        headers={"X-No-Cache": "true"},
        json={
            "model": "auto",
            "messages": [{"role": "user", "content": "Hello, what is a socket?"}],
        },
    )
    assert simple_resp.status_code == 200
    assert "X-Auto-Route" in simple_resp.headers
    assert "X-Auto-Complexity" in simple_resp.headers
    assert "simple" in simple_resp.headers["X-Auto-Complexity"].lower()
    assert float(simple_resp.headers.get("X-Auto-Saved-USD", 0.0)) >= 0.0

    # 2. Complex reasoning prompt with code fence and architectural keywords -> routes to frontier model
    complex_prompt = (
        "We are designing a distributed log-structured merge tree storage engine with concurrency deadlock issues. "
        "Here is the compaction algorithm: ```def compact_segments(): pass``` "
        "Please derive a mathematical proof and refactor the architecture step-by-step."
    )
    complex_resp = client.post(
        "/v1/chat/completions",
        headers={"X-No-Cache": "true"},
        json={
            "model": "auto",
            "messages": [{"role": "user", "content": complex_prompt}],
        },
    )
    assert complex_resp.status_code == 200
    assert "X-Auto-Route" in complex_resp.headers
    assert "complex" in complex_resp.headers["X-Auto-Complexity"].lower()


@pytest.mark.asyncio
async def test_provider_discovery_and_banner():
    info = await ProviderDiscovery.discover()
    assert "cloud" in info
    assert "local" in info
    assert "sandbox_mode" in info

    banner = format_startup_banner(info, host="127.0.0.1", port=8000)
    assert "SentinelGate" in banner
    assert "http://localhost:8000/v1" in banner
    assert "HUD Dashboard" in banner
