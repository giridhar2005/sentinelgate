from __future__ import annotations

import io
import logging
import os
import sqlite3

import httpx
import pytest
import respx
from starlette.testclient import TestClient

from app.core.config import AppConfig
from app.core.security import LogSanitizationFilter, reset_ip_failures
from app.main import check_demo_mode_constraints, create_app


@pytest.fixture(autouse=True)
def mock_external_providers():
    with respx.mock(assert_all_called=False) as respx_m:
        respx_m.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "chatcmpl-auth-test",
                    "object": "chat.completion",
                    "model": "gpt-4o-mini",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "pong"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
                },
            )
        )
        respx_m.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "msg_test_auth",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "pong"}],
                    "model": "claude-3-5-sonnet-20241022",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 5, "output_tokens": 5},
                },
            )
        )
        yield


def get_all_testable_routes(app):
    routes = []
    for r in app.routes:
        if hasattr(r, "original_router"):
            for sub_r in r.original_router.routes:
                if hasattr(sub_r, "path") and hasattr(sub_r, "methods"):
                    routes.append(sub_r)
        elif hasattr(r, "path") and hasattr(r, "methods"):
            routes.append(r)

    testable = []
    for r in routes:
        path = r.path
        if path == "/health":
            continue
        methods = [m for m in r.methods if m not in ("HEAD", "OPTIONS")]
        if not methods:
            continue
        method = "POST" if "POST" in methods else methods[0]
        test_path = path.replace("{key_val}", "sg-test-dummy-key").replace(
            "{key_id}", "sg-test-dummy-key"
        )
        testable.append((test_path, method))

    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for p, m in testable:
        if (p, m) not in seen:
            seen.add((p, m))
            deduped.append((p, m))
    return deduped


_app_for_enumeration = create_app()
ALL_ROUTES = get_all_testable_routes(_app_for_enumeration)


@pytest.fixture
def clean_client(test_app):
    _, app = test_app
    # Create clean TestClient without any default headers
    client = TestClient(app)
    new_vk = app.state.virtual_keys.create_key("auth-acceptance-app")
    valid_key = new_vk["key"]
    reset_ip_failures("testclient")
    reset_ip_failures("127.0.0.1")
    return client, app, valid_key


@pytest.mark.parametrize("path,method", ALL_ROUTES)
def test_all_routes_reject_unauthenticated_request(path, method, clean_client):
    client, _, _ = clean_client
    reset_ip_failures("testclient")
    reset_ip_failures("127.0.0.1")

    kwargs = {}
    if method in ("POST", "PUT", "PATCH"):
        kwargs["json"] = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}

    resp = client.request(method, path, **kwargs)
    assert resp.status_code == 401, (
        f"Route {method} {path} allowed unauthenticated access (status: {resp.status_code})"
    )


@pytest.mark.parametrize("path,method", ALL_ROUTES)
def test_all_routes_reject_garbage_token(path, method, clean_client):
    client, _, _ = clean_client
    reset_ip_failures("testclient")
    reset_ip_failures("127.0.0.1")

    headers = {"Authorization": "Bearer totally-invalid-garbage-token-999"}
    kwargs = {"headers": headers}
    if method in ("POST", "PUT", "PATCH"):
        kwargs["json"] = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}

    resp = client.request(method, path, **kwargs)
    assert resp.status_code == 401, (
        f"Route {method} {path} allowed garbage token (status: {resp.status_code})"
    )


@pytest.mark.parametrize("path,method", ALL_ROUTES)
def test_all_routes_allow_valid_virtual_key(path, method, clean_client):
    client, _, valid_key = clean_client
    reset_ip_failures("testclient")
    reset_ip_failures("127.0.0.1")

    headers = {"Authorization": f"Bearer {valid_key}"}
    kwargs = {"headers": headers}
    if method in ("POST", "PUT", "PATCH"):
        kwargs["json"] = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}

    resp = client.request(method, path, **kwargs)
    assert resp.status_code not in (401, 403), (
        f"Route {method} {path} rejected valid virtual key with status {resp.status_code}"
    )


@pytest.mark.parametrize("path,method", ALL_ROUTES)
def test_all_routes_allow_admin_token(path, method, clean_client):
    client, _, _ = clean_client
    reset_ip_failures("testclient")
    reset_ip_failures("127.0.0.1")

    headers = {"Authorization": "Bearer test-admin-token"}
    kwargs = {"headers": headers}
    if method in ("POST", "PUT", "PATCH"):
        kwargs["json"] = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}

    resp = client.request(method, path, **kwargs)
    assert resp.status_code not in (401, 403), (
        f"Route {method} {path} rejected admin token with status {resp.status_code}"
    )


def test_health_route_is_public(clean_client):
    client, _, _ = clean_client
    resp = client.get("/health")
    assert resp.status_code == 200


def test_cors_default_is_not_wildcard(clean_client):
    client, _, _ = clean_client
    resp = client.get("/health", headers={"Origin": "https://attacker.evil.com"})
    allow_origin = resp.headers.get("access-control-allow-origin")
    assert allow_origin != "*", f"CORS allowed wildcard '*' origin: {allow_origin}"


def test_virtual_keys_hashed_at_rest(clean_client):
    _, app, valid_key = clean_client
    db_path = app.state.virtual_keys.db_path

    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("SELECT * FROM virtual_keys")
        rows = cursor.fetchall()
        assert len(rows) > 0
        for row in rows:
            for col in row:
                if isinstance(col, str):
                    assert valid_key not in col, (
                        f"Raw key {valid_key} was found in plaintext in database column: {col}"
                    )


def test_demo_mode_refuses_to_start_with_external_host():
    os.environ["DEMO_MODE"] = "true"
    try:
        cfg = AppConfig()
        cfg.server.host = "0.0.0.0"
        with pytest.raises(ValueError, match="can only be bound to 127.0.0.1 or localhost"):
            check_demo_mode_constraints(cfg)
    finally:
        os.environ.pop("DEMO_MODE", None)


def test_demo_mode_refuses_to_start_with_real_keys():
    os.environ["DEMO_MODE"] = "true"
    os.environ["OPENAI_API_KEY"] = "sk-proj-actual-real-production-api-key-12345"
    try:
        cfg = AppConfig()
        cfg.server.host = "127.0.0.1"
        with pytest.raises(ValueError, match="real provider keys detected"):
            check_demo_mode_constraints(cfg)
    finally:
        os.environ.pop("DEMO_MODE", None)
        os.environ.pop("OPENAI_API_KEY", None)


def test_request_body_size_limit(clean_client):
    client, _, _ = clean_client
    headers = {"Authorization": "Bearer test-admin-token", "Content-Length": str(20 * 1024 * 1024)}
    resp = client.post("/v1/chat/completions", headers=headers, json={"data": "x" * 100})
    assert resp.status_code == 413


def test_per_ip_rate_limiting_on_auth_failures(clean_client):
    client, _, _ = clean_client
    reset_ip_failures("testclient")

    for _ in range(10):
        resp = client.get("/v1/models", headers={"Authorization": "Bearer bad-token"})
        assert resp.status_code == 401

    # 11th request should be rate-limited with 429
    resp = client.get("/v1/models", headers={"Authorization": "Bearer bad-token"})
    assert resp.status_code == 429
    assert "Too many failed authentication attempts" in resp.text
    reset_ip_failures("testclient")


def test_keys_and_tokens_not_in_logs():
    test_logger = logging.getLogger("sentinelgate.audit_log_test")
    test_logger.setLevel(logging.INFO)
    test_logger.addFilter(LogSanitizationFilter())

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    test_logger.addHandler(handler)

    secret_key = "sg-live-testuser-1234567890abcdef"
    secret_openai = "sk-proj-9876543210fedcba"

    test_logger.info(f"Connecting with Authorization: Bearer {secret_key}")
    test_logger.info(f"Upstream provider key configured: {secret_openai}")

    handler.flush()
    log_output = stream.getvalue()

    assert secret_key not in log_output
    assert secret_openai not in log_output
    assert "[REDACTED_CREDENTIAL]" in log_output
