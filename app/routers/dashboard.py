from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter(tags=["Observability & Dashboard"])


@router.get("/dashboard", response_class=HTMLResponse)
async def serve_dashboard():
    candidates = ["dashboard.html", "app/templates/dashboard.html"]
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
    return JSONResponse(content=summary)


@router.get("/v1/providers/health")
async def provider_health(request: Request):
    gateway_router = request.app.state.router
    return JSONResponse(content={"providers": gateway_router.get_provider_statuses()})
