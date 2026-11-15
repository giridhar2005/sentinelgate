from __future__ import annotations

import sqlite3


def test_dashboard_and_metrics_endpoints(test_app):
    client, _ = test_app
    dash_resp = client.get("/dashboard")
    assert dash_resp.status_code == 200
    assert "SentinelGate" in dash_resp.text
    assert "HUD" in dash_resp.text

    metrics_resp = client.get("/api/metrics")
    assert metrics_resp.status_code == 200
    m_data = metrics_resp.json()
    assert "total_requests" in m_data
    assert "cache_hit_rate" in m_data
    assert "p50_latency_ms" in m_data


def test_golden_dataset_export(test_app):
    client, app = test_app
    evals = app.state.evals

    with sqlite3.connect(evals.db_path) as conn:
        conn.execute(
            """
            INSERT INTO shadow_evals (
                request_id, original_model, shadow_model, prompt, original_response,
                shadow_response, judge_score, judge_reasoning, is_win, timestamp
            ) VALUES ('req-golden-1', 'gpt-4o', 'llama-3.1-8b', 'Explain Python GIL', 'GIL is...', 'Python GIL is...', 9, 'High parity', 1, 1000.0)
            """
        )

    resp = client.get("/v1/evals/export?limit=10")
    assert resp.status_code == 200
    dataset = resp.json()["dataset"]
    assert len(dataset) >= 1
    assert dataset[0]["score"] == 9
    assert dataset[0]["parity_win"] is True
    assert "Explain Python GIL" in dataset[0]["input"]


def test_cache_warming_endpoint(test_app):
    client, app = test_app

    warm_resp = client.post(
        "/v1/cache/warm",
        json=[
            {
                "prompt": "What is SentinelGate?",
                "response": "SentinelGate is a zero-bloat OpenAI-compatible LLM Gateway.",
                "model": "gpt-4o-mini",
            }
        ],
    )
    assert warm_resp.status_code == 200
    assert warm_resp.json()["prewarmed_count"] == 1

    # Exact query should hit cache immediately
    q_resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "What is SentinelGate?"}],
        },
    )
    assert q_resp.status_code == 200
    assert q_resp.headers.get("X-Cache") == "HIT"
    assert (
        "zero-bloat OpenAI-compatible LLM Gateway"
        in q_resp.json()["choices"][0]["message"]["content"]
    )


def test_export_traces_endpoints(test_app):
    client, app = test_app
    evals = app.state.evals

    with sqlite3.connect(evals.db_path) as conn:
        conn.execute(
            """
            INSERT INTO request_metrics (
                request_id, model, provider, latency_ms, cache_hit, prompt_tokens,
                completion_tokens, cost_usd, cost_saved_usd, timestamp, ttft_ms,
                status, prompt_snippet, similarity_score, timing_breakdown, response_snippet
            ) VALUES ('req-exp-1', 'gpt-4o', 'openai', 45.2, 0, 10, 20, 0.0003, 0.0, 1000.0, 30.0,
                     'UPSTREAM', 'Test export prompt', 0.0, '{}', 'Test export answer')
            """
        )

    jsonl_resp = client.get("/api/export/traces.jsonl?limit=5")
    assert jsonl_resp.status_code == 200
    assert "application/x-ndjson" in jsonl_resp.headers["content-type"]
    assert "Test export prompt" in jsonl_resp.text
    assert "Test export answer" in jsonl_resp.text

    csv_resp = client.get("/api/export/traces.csv?limit=5")
    assert csv_resp.status_code == 200
    assert "text/csv" in csv_resp.headers["content-type"]
    assert "req-exp-1" in csv_resp.text
    assert "Test export prompt" in csv_resp.text


def test_live_benchmark_and_otel_audit_endpoints(test_app):
    client, app = test_app

    # 1. Test live in-process benchmark execution
    bench_resp = client.post(
        "/api/benchmark/run",
        json={"concurrency": 5, "requests": 15, "test_type": "cache_hit"},
    )
    assert bench_resp.status_code == 200
    b_data = bench_resp.json()
    assert b_data["status"] == "success"
    assert b_data["total_requests"] == 15
    assert b_data["throughput_rps"] > 0
    assert "p50_ms" in b_data["latencies"]
    assert "process_rss_mb" in b_data

    # 2. Test guardrail audit logs endpoint
    evals = app.state.evals
    evals.log_guardrail_event(
        "req-test-audit", "TOOL_CALL_BLOCKED", "rm -rf / blocked", blocked=True
    )

    audit_resp = client.get("/api/guardrails/audit?limit=10")
    assert audit_resp.status_code == 200
    events = audit_resp.json()["events"]
    assert len(events) >= 1
    assert any(e["event_type"] == "TOOL_CALL_BLOCKED" for e in events)

    # 3. Test OTLP traces ring buffer endpoint
    otel_resp = client.get("/api/traces/otel?limit=10")
    assert otel_resp.status_code == 200
    assert "spans" in otel_resp.json()
