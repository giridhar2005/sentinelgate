"""Test suite for Zero-Dependency OpenTelemetry (OTLP) Exporter & W3C traceparent."""

import re

from starlette.testclient import TestClient

from app.core.telemetry_otlp import OTLPSpan, TraceContext
from app.main import create_app


def test_w3c_traceparent_parsing_and_child_derivation():
    """Verify W3C traceparent header parsing and child span generation."""
    incoming = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    ctx = TraceContext.from_header(incoming)

    assert ctx.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert ctx.parent_span_id == "00f067aa0ba902b7"
    assert ctx.trace_flags == "01"
    assert len(ctx.span_id) == 16
    assert ctx.span_id != "00f067aa0ba902b7"

    child = ctx.create_child()
    assert child.trace_id == ctx.trace_id
    assert child.parent_span_id == ctx.span_id
    assert child.span_id != ctx.span_id


def test_w3c_traceparent_fresh_generation():
    """Verify fresh generation when header is missing or malformed."""
    ctx = TraceContext.from_header(None)
    assert len(ctx.trace_id) == 32
    assert len(ctx.span_id) == 16
    assert ctx.parent_span_id is None

    malformed = TraceContext.from_header("invalid-trace-header")
    assert len(malformed.trace_id) == 32
    assert len(malformed.span_id) == 16


def test_otlp_span_json_schema():
    """Verify OpenTelemetry v1 OTLP JSON schema conversion."""
    span = OTLPSpan(
        name="sentinelgate.request",
        trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
        span_id="00f067aa0ba902b7",
        parent_span_id="5fb397be34d23b0f",
        attributes={
            "http.method": "POST",
            "llm.request.model": "gpt-4o-mini",
            "llm.cost_usd": 0.00015,
            "cache.hit": False,
        },
        status_code=1,
    )

    data = span.to_dict()
    assert data["name"] == "sentinelgate.request"
    assert data["traceId"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert data["spanId"] == "00f067aa0ba902b7"
    assert data["parentSpanId"] == "5fb397be34d23b0f"
    assert data["status"]["code"] == 1

    attr_map = {attr["key"]: attr["value"] for attr in data["attributes"]}
    assert attr_map["http.method"]["stringValue"] == "POST"
    assert attr_map["llm.request.model"]["stringValue"] == "gpt-4o-mini"
    assert attr_map["llm.cost_usd"]["doubleValue"] == 0.00015
    assert attr_map["cache.hit"]["boolValue"] is False


def test_traceparent_header_propagation_on_chat_completions(tmp_path):
    """Test POST /v1/chat/completions preserves trace_id and returns traceparent."""
    db_file = str(tmp_path / "test_traceparent.db")
    app = create_app(db_path=db_file)
    client = TestClient(app, headers={"Authorization": "Bearer admin-secret-token"})

    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    parent_span = "00f067aa0ba902b7"
    incoming_traceparent = f"00-{trace_id}-{parent_span}-01"

    req_body = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "Hello via distributed trace"}],
    }

    res = client.post(
        "/v1/chat/completions",
        json=req_body,
        headers={"traceparent": incoming_traceparent},
    )
    assert res.status_code == 200
    out_traceparent = res.headers.get("traceparent")
    assert out_traceparent is not None

    # Must preserve the same 32-hex trace_id
    pattern = rf"^00-{trace_id}-([0-9a-f]{{16}})-01$"
    assert re.match(pattern, out_traceparent) is not None
    assert res.headers.get("X-Agent-Firewall-Blocked") == "false"
