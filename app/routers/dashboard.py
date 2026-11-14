from __future__ import annotations

import os
import time
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

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
    summary["chaos"] = {
        "enabled": getattr(gateway_router, "chaos_enabled", False),
        "target_provider": getattr(gateway_router, "chaos_target_provider", "openai"),
        "status_code": getattr(gateway_router, "chaos_status_code", 429),
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
    new_state = (not gateway_router.chaos_enabled) if (body is None or body.enabled is None) else body.enabled
    target = body.target_provider if (body and body.target_provider) else gateway_router.chaos_target_provider
    status_code = body.status_code if (body and body.status_code) else gateway_router.chaos_status_code

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
        cand_res, cand_provider, cand_model = await gateway_router.execute_request(req_data, stream=False)
        cand_lat_ms = (time.perf_counter() - t0) * 1000.0
    except Exception as e:
        return JSONResponse(content={"error": f"Candidate model execution failed: {e}"}, status_code=502)

    cand_text = cand_res.get("choices", [{}])[0].get("message", {}).get("content", "")
    usage = cand_res.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", max(1, len(prompt) // 4))
    completion_tokens = usage.get("completion_tokens", max(1, len(cand_text) // 4))

    # 2. Cost calculations
    orig_provider, orig_mod, _ = gateway_router.resolve_model(payload.original_model)
    cand_cost = gateway_router.get_model_cost(cand_provider, cand_model, prompt_tokens, completion_tokens)

    orig_cost = payload.original_cost_usd
    if orig_cost is None or orig_cost <= 0.0:
        orig_cost = gateway_router.get_model_cost(orig_provider, orig_mod, prompt_tokens, completion_tokens)

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
