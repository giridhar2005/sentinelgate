from __future__ import annotations

import asyncio
import logging
import platform
import resource
import sqlite3
import time
from typing import Optional

import numpy as np
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger("sentinelgate.dashboard_api")

api_router = APIRouter(tags=["Observability & Dashboard APIs"])


class BenchmarkRunRequest(BaseModel):
    concurrency: Optional[int] = 10
    requests: Optional[int] = 50
    test_type: Optional[str] = "cache_hit"


@api_router.post("/api/benchmark/run")
async def run_live_benchmark(request: Request, body: Optional[BenchmarkRunRequest] = None):
    """Execute high-speed in-process micro-benchmark and return real latency percentiles."""
    req_body = body or BenchmarkRunRequest()
    concurrency = max(1, min(50, req_body.concurrency or 10))
    n_requests = max(5, min(200, req_body.requests or 50))
    test_type = req_body.test_type or "cache_hit"

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


@api_router.get("/api/guardrails/audit")
async def get_guardrail_audit_logs(request: Request, limit: int = 50):
    """Return live audit events for Agent Security Firewall and Guardrails."""
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


@api_router.get("/api/traces/otel")
async def get_otel_spans(limit: int = 50):
    """Return recent OpenTelemetry spans from the in-memory ring buffer."""
    from sentinelgate.routers.v1 import otlp_exporter

    return JSONResponse(content={"spans": otlp_exporter.get_recent_spans(limit=limit)})
