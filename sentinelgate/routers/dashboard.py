from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

logger = logging.getLogger("sentinelgate.dashboard")

router = APIRouter(tags=["Observability & Dashboard"])


class ChaosConfigRequest(BaseModel):
    enabled: Optional[bool] = None
    target_provider: Optional[str] = "openai"
    status_code: Optional[int] = 429


class ReplayRequest(BaseModel):
    prompt: str
    original_model: str = "gpt-4o"
    candidate_model: str = "groq/llama-3.1-8b-instant"
    original_latency_ms: Optional[float] = None
    original_cost_usd: Optional[float] = None
    original_response: Optional[str] = None


@router.get("/dashboard", response_class=HTMLResponse)
async def serve_dashboard():
    pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.join(pkg_dir, "templates", "dashboard.html"),
        "frontend/dist/index.html",
        "/app/frontend/dist/index.html",
        "dashboard.html",
        "app/templates/dashboard.html",
        "sentinelgate/templates/dashboard.html",
    ]
    for path in candidates:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>SentinelGate Dashboard not found</h1>", status_code=404)


@router.get("/api/metrics")
async def get_metrics(request: Request):
    evals = request.app.state.evals
    budget = request.app.state.budget
    gateway_router = request.app.state.router

    summary = evals.get_metrics_summary()
    spent_today, req_cnt_today = budget.get_today_spend()
    limit = budget.daily_limit_usd
    percent = round((spent_today / limit * 100.0), 1) if limit > 0 else 0.0

    summary["budget"] = {
        "spent_today_usd": spent_today,
        "limit_usd": limit,
        "percent_used": percent,
        "is_allowed": spent_today < limit,
        "request_count_today": req_cnt_today,
    }
    summary["providers"] = gateway_router.get_provider_statuses()
    summary["chaos"] = {
        "enabled": getattr(gateway_router, "chaos_enabled", False),
        "target_provider": getattr(gateway_router, "chaos_target_provider", "openai"),
        "status_code": getattr(gateway_router, "chaos_status_code", 429),
    }
    summary["hedging"] = {
        "triggered": getattr(gateway_router, "hedged_triggered_count", 0),
        "primary_won": getattr(gateway_router, "hedged_primary_won_count", 0),
        "secondary_won": getattr(gateway_router, "hedged_secondary_won_count", 0),
    }
    return JSONResponse(content=summary)


@router.get("/v1/providers/health")
async def provider_health(request: Request):
    gateway_router = request.app.state.router
    return JSONResponse(content={"providers": gateway_router.get_provider_statuses()})


@router.get("/api/chaos")
async def get_chaos_status(request: Request):
    gateway_router = request.app.state.router
    return JSONResponse(
        content={
            "enabled": gateway_router.chaos_enabled,
            "target_provider": gateway_router.chaos_target_provider,
            "status_code": gateway_router.chaos_status_code,
        }
    )


@router.post("/api/chaos")
async def toggle_chaos(request: Request, body: Optional[ChaosConfigRequest] = None):
    gateway_router = request.app.state.router
    new_state = (
        (not gateway_router.chaos_enabled)
        if (body is None or body.enabled is None)
        else body.enabled
    )
    target = (
        body.target_provider
        if (body and body.target_provider)
        else gateway_router.chaos_target_provider
    )
    status_code = (
        body.status_code if (body and body.status_code) else gateway_router.chaos_status_code
    )

    gateway_router.set_chaos(enabled=new_state, target_provider=target, status_code=status_code)
    return JSONResponse(
        content={
            "enabled": gateway_router.chaos_enabled,
            "target_provider": gateway_router.chaos_target_provider,
            "status_code": gateway_router.chaos_status_code,
            "message": f"Chaos fault injection {'enabled' if new_state else 'disabled'} for provider '{target}' (HTTP {status_code})",
        }
    )


@router.post("/api/replay")
async def shadow_replay_estimator(request: Request, payload: ReplayRequest):
    """Replay prompt against a cheaper/candidate model and calculate projected monthly unit economics."""
    gateway_router = request.app.state.router
    prompt = payload.prompt.strip()
    if not prompt:
        return JSONResponse(content={"error": "Prompt cannot be empty"}, status_code=400)

    # 1. Execute candidate model
    t0 = time.perf_counter()
    req_data = {
        "model": payload.candidate_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
    }
    try:
        cand_res, cand_provider, cand_model = await gateway_router.execute_request(
            req_data, stream=False
        )
        cand_lat_ms = (time.perf_counter() - t0) * 1000.0
    except Exception as e:
        logger.warning(f"Candidate model execution failed: {e}")
        return JSONResponse(content={"error": "Candidate model execution failed."}, status_code=502)

    cand_text = cand_res.get("choices", [{}])[0].get("message", {}).get("content", "")
    usage = cand_res.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", max(1, len(prompt) // 4))
    completion_tokens = usage.get("completion_tokens", max(1, len(cand_text) // 4))

    # 2. Cost calculations
    orig_provider, orig_mod, _ = gateway_router.resolve_model(payload.original_model)
    cand_cost = gateway_router.get_model_cost(
        cand_provider, cand_model, prompt_tokens, completion_tokens
    )

    orig_cost = payload.original_cost_usd
    if orig_cost is None or orig_cost <= 0.0:
        orig_cost = gateway_router.get_model_cost(
            orig_provider, orig_mod, prompt_tokens, completion_tokens
        )

    cost_saved_per_req = max(0.0, orig_cost - cand_cost)
    savings_percent = round((cost_saved_per_req / orig_cost * 100.0), 1) if orig_cost > 0 else 0.0

    monthly_volume = 100_000
    orig_monthly = round(orig_cost * monthly_volume, 2)
    cand_monthly = round(cand_cost * monthly_volume, 2)
    projected_monthly_savings = round(cost_saved_per_req * monthly_volume, 2)

    orig_lat = payload.original_latency_ms or 0.0
    lat_diff_ms = round(cand_lat_ms - orig_lat, 2)

    return JSONResponse(
        content={
            "prompt": prompt,
            "original_model": payload.original_model,
            "original_provider": orig_provider,
            "original_response": payload.original_response or "",
            "original_latency_ms": round(orig_lat, 2),
            "original_cost_per_req": round(orig_cost, 6),
            "candidate_model": payload.candidate_model,
            "candidate_provider": cand_provider,
            "candidate_response": cand_text,
            "candidate_latency_ms": round(cand_lat_ms, 2),
            "candidate_cost_per_req": round(cand_cost, 6),
            "latency_diff_ms": lat_diff_ms,
            "cost_saved_per_req": round(cost_saved_per_req, 6),
            "savings_percent": savings_percent,
            "projected_monthly_savings_100k": projected_monthly_savings,
            "projected_orig_monthly_usd": orig_monthly,
            "projected_cand_monthly_usd": cand_monthly,
            "tokens": {
                "prompt": prompt_tokens,
                "completion": completion_tokens,
                "total": prompt_tokens + completion_tokens,
            },
        }
    )


@router.get("/api/export/traces.jsonl")
async def export_traces_jsonl(request: Request, limit: int = 500):
    """Export captured SQLite traces in OpenAI Fine-Tuning & Evaluation JSONL format."""
    import sqlite3

    evals = request.app.state.evals
    with sqlite3.connect(evals.db_path, timeout=15.0) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT request_id, model, provider, latency_ms, cost_usd, cost_saved_usd,
                   prompt_snippet, response_snippet, timestamp, status
            FROM request_metrics ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()

    lines = []
    for r in rows:
        item = {
            "messages": [
                {"role": "user", "content": r["prompt_snippet"] or ""},
                {"role": "assistant", "content": r["response_snippet"] or ""},
            ],
            "metadata": {
                "request_id": r["request_id"],
                "model": r["model"],
                "provider": r["provider"],
                "latency_ms": r["latency_ms"],
                "cost_usd": r["cost_usd"],
                "status": r["status"],
                "timestamp": r["timestamp"],
            },
        }
        lines.append(json.dumps(item))

    return Response(
        content="\n".join(lines),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": "attachment; filename=sentinelgate_eval_traces.jsonl"},
    )


@router.get("/api/export/traces.csv")
async def export_traces_csv(request: Request, limit: int = 500):
    """Export captured SQLite traces as CSV for analytical inspection."""
    import csv
    import io
    import sqlite3

    evals = request.app.state.evals
    with sqlite3.connect(evals.db_path, timeout=15.0) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT request_id, timestamp, model, provider, status, latency_ms,
                   cost_usd, cost_saved_usd, prompt_tokens, completion_tokens, prompt_snippet
            FROM request_metrics ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "request_id",
            "timestamp",
            "model",
            "provider",
            "status",
            "latency_ms",
            "cost_usd",
            "cost_saved_usd",
            "prompt_tokens",
            "completion_tokens",
            "prompt_snippet",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r["request_id"],
                r["timestamp"],
                r["model"],
                r["provider"],
                r["status"],
                r["latency_ms"],
                r["cost_usd"],
                r["cost_saved_usd"],
                r["prompt_tokens"],
                r["completion_tokens"],
                (r["prompt_snippet"] or "").replace("\n", " ")[:120],
            ]
        )

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=sentinelgate_traces.csv"},
    )


class BenchmarkRunRequest(BaseModel):
    concurrency: Optional[int] = 10
    requests: Optional[int] = 50
    test_type: Optional[str] = "cache_hit"


@router.post("/api/benchmark/run")
async def run_live_benchmark(request: Request, body: Optional[BenchmarkRunRequest] = None):
    """Execute high-speed in-process micro-benchmark and return real latency percentiles."""
    import asyncio
    import platform
    import resource

    import numpy as np

    req_body = body or BenchmarkRunRequest()
    concurrency = max(1, min(50, req_body.concurrency or 10))
    n_requests = max(5, min(200, req_body.requests or 50))
    test_type = req_body.test_type or "cache_hit"

    # Seed query if cache hit benchmark
    if test_type == "cache_hit":
        cache = request.app.state.cache
        cache.store(
            messages=[
                {"role": "user", "content": "What is connection pooling in async HTTP clients?"}
            ],
            model="gpt-4o-mini",
            response={
                "id": "bench-seed",
                "choices": [{"message": {"content": "Connection pooling reuses active sockets."}}],
            },
        )

    sem = asyncio.Semaphore(concurrency)
    latencies: list[float] = []

    async def _worker_task(idx: int):
        async with sem:
            t0 = time.perf_counter()
            if test_type == "cache_hit":
                # Look up semantic cache directly
                _ = request.app.state.cache.lookup(
                    messages=[
                        {
                            "role": "user",
                            "content": "Explain connection pooling in async HTTP clients.",
                        }
                    ],
                    model="gpt-4o-mini",
                )
            else:
                # Sandbox completion
                _ = await request.app.state.router.execute_request(
                    {
                        "model": "gpt-4o-mini",
                        "messages": [{"role": "user", "content": f"Compute test payload {idx}"}],
                    },
                    stream=False,
                )
            lat_ms = (time.perf_counter() - t0) * 1000.0
            latencies.append(lat_ms)

    t_bench_start = time.perf_counter()
    tasks = [_worker_task(i) for i in range(n_requests)]
    await asyncio.gather(*tasks)
    total_elapsed = time.perf_counter() - t_bench_start

    arr = np.array(latencies) if latencies else np.array([0.0])
    raw_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mb = round(raw_rss / (1024 * 1024) if platform.system() == "Darwin" else raw_rss / 1024, 2)

    return JSONResponse(
        content={
            "status": "success",
            "test_type": test_type,
            "total_requests": n_requests,
            "concurrency": concurrency,
            "duration_seconds": round(total_elapsed, 4),
            "throughput_rps": round(n_requests / max(0.0001, total_elapsed), 1),
            "latencies": {
                "p50_ms": round(float(np.percentile(arr, 50)), 2),
                "p90_ms": round(float(np.percentile(arr, 90)), 2),
                "p95_ms": round(float(np.percentile(arr, 95)), 2),
                "p99_ms": round(float(np.percentile(arr, 99)), 2),
                "avg_ms": round(float(np.mean(arr)), 2),
                "min_ms": round(float(np.min(arr)), 2),
                "max_ms": round(float(np.max(arr)), 2),
            },
            "process_rss_mb": rss_mb,
        }
    )


@router.get("/api/guardrails/audit")
async def get_guardrail_audit_logs(request: Request, limit: int = 50):
    """Return live audit events for Agent Security Firewall and Guardrails."""
    import sqlite3

    evals = request.app.state.evals
    evals.pipeline._flush_sync_drain()
    with sqlite3.connect(evals.db_path, timeout=15.0) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, request_id, event_type, threat_detail, blocked, timestamp
            FROM guardrail_events
            ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return JSONResponse(content={"events": [dict(r) for r in rows]})


@router.get("/api/traces/otel")
async def get_otel_spans(limit: int = 50):
    """Return recent OpenTelemetry spans from the in-memory ring buffer."""
    from sentinelgate.routers.v1 import otlp_exporter

    return JSONResponse(content={"spans": otlp_exporter.get_recent_spans(limit=limit)})
