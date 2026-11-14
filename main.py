from __future__ import annotations

import contextlib
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from budget import BudgetManager, VirtualKeyManager
from cache import SemanticCache
from evals import EvalsManager
from guardrails import (
    ContextCompactor,
    InjectionDefense,
    JSONSelfHealer,
    PIIGuardrail,
    PromptOptimizer,
)
from router import ProviderRouter

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("sentinelgate")

CONFIG_PATH = os.getenv("SENTINELGATE_CONFIG", "config.yaml")
DB_PATH = os.getenv("SENTINELGATE_DB", "sentinelgate.db")
DAILY_BUDGET = float(os.getenv("SENTINELGATE_DAILY_BUDGET", "10.0"))

router = ProviderRouter(config_path=CONFIG_PATH)
cache = SemanticCache(db_path=DB_PATH)
evals = EvalsManager(db_path=DB_PATH)
budget = BudgetManager(db_path=DB_PATH, daily_limit_usd=DAILY_BUDGET)
virtual_keys = VirtualKeyManager(db_path=DB_PATH)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing SentinelGate Gateway...")
    await router.get_client()
    yield
    logger.info("Shutting down SentinelGate Gateway...")
    await router.close()


app = FastAPI(
    title="SentinelGate",
    description="Zero-bloat, OpenAI-compatible LLM Gateway with semantic caching and failover.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatMessage(BaseModel):
    role: str
    content: str
    name: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: str = "gpt-4o-mini"
    messages: List[Dict[str, Any]]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False
    response_format: Optional[Dict[str, Any]] = None
    hedge: Optional[bool] = False


@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "SentinelGate", "version": "0.1.0", "timestamp": time.time()}


@app.get("/v1/models")
async def list_models():
    return {"object": "list", "data": router.get_all_models()}


@app.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    background_tasks: BackgroundTasks,
    x_no_cache: Optional[str] = Header(None, alias="X-No-Cache"),
    x_hedge: Optional[str] = Header(None, alias="X-Hedge"),
    authorization: Optional[str] = Header(None),
):
    # Multi-tenant virtual key validation & sliding-window rate limiting
    if authorization and "Bearer sg-" in authorization:
        v_key = authorization.split("Bearer ", 1)[1].strip()
        is_valid, err_msg = virtual_keys.verify_and_rate_limit(v_key)
        if not is_valid:
            status_code = 429 if "Rate limit" in (err_msg or "") else 401
            raise HTTPException(status_code=status_code, detail=err_msg)

    # Prompt injection & adversarial override shield
    is_safe, threat_reason = InjectionDefense.inspect(req.messages)
    if not is_safe:
        logger.warning(f"Prompt injection blocked: {threat_reason}")
        raise HTTPException(
            status_code=400,
            detail={"error": "Prompt Security Violation: Adversarial injection detected", "threat": threat_reason},
        )

    start_time = time.time()
    req_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    raw_dict = req.model_dump(exclude_unset=True)

    # 0. Zero-Loss Compaction & Prompt Optimization
    compacted_messages = ContextCompactor.compact(req.messages)
    optimized_messages = PromptOptimizer.optimize_messages(compacted_messages)

    # 1. PII Masking Guardrail
    masked_messages, pii_mapping = PIIGuardrail.mask_messages(optimized_messages)
    active_req = dict(raw_dict)
    active_req["messages"] = masked_messages

    requested_model = req.model
    provider, internal_model, _ = router.resolve_model(requested_model, masked_messages)

    # 2. Semantic Cache Check (for non-streaming queries)
    if not req.stream:
        cached_result = cache.lookup(
            messages=masked_messages,
            model=requested_model,
            bypass_header=x_no_cache,
        )
        if cached_result:
            cached_resp, similarity = cached_result
            latency_ms = (time.time() - start_time) * 1000.0

            # Restore any PII in cached response
            final_resp = PIIGuardrail.restore_in_response(cached_resp, pii_mapping)
            final_resp["id"] = req_id

            usage = final_resp.get("usage", {})
            in_tokens = usage.get("prompt_tokens", 0)
            out_tokens = usage.get("completion_tokens", 0)

            # Saved USD cost
            cost_saved = router.get_model_cost(provider, internal_model, in_tokens, out_tokens)

            evals.log_request(
                request_id=req_id,
                model=requested_model,
                provider=f"{provider}(cached)",
                latency_ms=latency_ms,
                cache_hit=True,
                prompt_tokens=in_tokens,
                completion_tokens=out_tokens,
                cost_usd=0.0,
                cost_saved_usd=cost_saved,
            )
            return JSONResponse(content=final_resp)

    # 2.5 Common-Sense Budget Circuit Breaker: Halt upstream queries if daily limit is exceeded
    is_budget_ok, spent_today, budget_limit = budget.check_budget()
    if not is_budget_ok:
        logger.warning(f"Budget circuit breaker tripped! Spent: ${spent_today:.4f} / ${budget_limit:.2f}")
        raise HTTPException(
            status_code=429,
            detail=(
                f"SentinelGate Daily Budget Exceeded: Spent ${spent_today:.4f} / ${budget_limit:.2f} USD today. "
                "Halting upstream queries to protect against cost overruns."
            ),
        )

    # 3. Direct REST execution with Key Rotation, Failover, or Speculative Hedging
    try:
        use_hedge = (req.hedge or (x_hedge and x_hedge.lower() == "true")) and not req.stream
        if use_hedge:
            upstream_res, chosen_provider, chosen_model = await router.execute_hedged_request(active_req)
        else:
            upstream_res, chosen_provider, chosen_model = await router.execute_request(
                active_req, stream=bool(req.stream)
            )
    except Exception as e:
        logger.error(f"Execution failed for {requested_model}: {e}")
        raise HTTPException(status_code=502, detail=f"All upstream providers failed: {e}")

    # 4. Handle SSE Streaming Response
    if req.stream:
        async def event_generator():
            first_chunk = True
            try:
                async for chunk in upstream_res:
                    if first_chunk:
                        ttft = (time.time() - start_time) * 1000.0
                        evals.log_request(
                            request_id=req_id,
                            model=requested_model,
                            provider=chosen_provider,
                            latency_ms=ttft,
                            cache_hit=False,
                            prompt_tokens=0,
                            completion_tokens=0,
                            cost_usd=0.0,
                            ttft_ms=ttft,
                        )
                        first_chunk = False
                    if pii_mapping:
                        chunk = PIIGuardrail.restore_text(chunk, pii_mapping)
                    yield chunk
            except Exception as stream_err:
                logger.error(f"Streaming error: {stream_err}")

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    # 5. Non-streaming Post-Processing
    # JSON Self-Healing
    if JSONSelfHealer.is_json_requested(active_req):
        upstream_res = await JSONSelfHealer.repair_response(
            router=router,
            request_data=active_req,
            initial_response=upstream_res,
        )

    # PII Restoration
    final_resp = PIIGuardrail.restore_in_response(upstream_res, pii_mapping)
    final_resp["id"] = req_id

    latency_ms = (time.time() - start_time) * 1000.0
    usage = final_resp.get("usage", {})
    in_tok = usage.get("prompt_tokens", max(1, len(str(req.messages)) // 4))
    out_tok = usage.get(
        "completion_tokens",
        max(1, len(final_resp.get("choices", [{}])[0].get("message", {}).get("content", "")) // 4),
    )

    cost_usd = router.get_model_cost(chosen_provider, chosen_model, in_tok, out_tok)

    # Store in Semantic Cache
    cache.store(
        messages=masked_messages,
        model=requested_model,
        response=final_resp,
        prompt_tokens=in_tok,
        completion_tokens=out_tok,
    )

    # Telemetry logging
    evals.log_request(
        request_id=req_id,
        model=requested_model,
        provider=chosen_provider,
        latency_ms=latency_ms,
        cache_hit=False,
        prompt_tokens=in_tok,
        completion_tokens=out_tok,
        cost_usd=cost_usd,
        cost_saved_usd=0.0,
    )

    # Record spend in Daily Budget Tracker
    budget.record_spend(cost_usd)

    # 6. Asynchronous 10% Shadow Evaluation
    if evals.should_sample_shadow(cache_hit=False):
        resp_text = (
            final_resp.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        background_tasks.add_task(
            evals.run_shadow_eval,
            router=router,
            request_id=req_id,
            original_model=requested_model,
            messages=masked_messages,
            original_response_text=resp_text,
        )

    return JSONResponse(content=final_resp)


class CreateKeyRequest(BaseModel):
    name: str
    daily_budget_usd: Optional[float] = 10.0
    rate_limit_rpm: Optional[int] = 60


@app.post("/v1/keys")
async def create_key(req: CreateKeyRequest):
    new_k = virtual_keys.create_key(
        name=req.name,
        daily_budget_usd=req.daily_budget_usd or 10.0,
        rate_limit_rpm=req.rate_limit_rpm or 60,
    )
    return JSONResponse(content=new_k)


@app.get("/v1/keys")
async def list_keys():
    return JSONResponse(content={"keys": virtual_keys.list_keys()})


class CacheWarmItem(BaseModel):
    prompt: str
    response: str
    model: Optional[str] = "gpt-4o-mini"


@app.post("/v1/cache/warm")
async def warm_cache(items: List[CacheWarmItem]):
    for item in items:
        cache.store(
            messages=[{"role": "user", "content": item.prompt}],
            model=item.model or "gpt-4o-mini",
            response={
                "id": f"chatcmpl-warm-{uuid.uuid4().hex[:8]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": item.model or "gpt-4o-mini",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": item.response},
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": len(item.prompt) // 4,
                    "completion_tokens": len(item.response) // 4,
                    "total_tokens": (len(item.prompt) + len(item.response)) // 4,
                },
            },
        )
    return JSONResponse(content={"status": "success", "prewarmed_count": len(items)})


@app.get("/v1/providers/health")
async def provider_health():
    return JSONResponse(content={"providers": router.get_provider_statuses()})


@app.get("/v1/evals/export")
async def export_evals(limit: Optional[int] = 100):
    return JSONResponse(content={"dataset": evals.export_golden_dataset(limit=limit or 100)})


@app.get("/dashboard", response_class=HTMLResponse)
async def serve_dashboard():
    dashboard_file = "dashboard.html"
    if os.path.exists(dashboard_file):
        with open(dashboard_file, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Dashboard file not found</h1>", status_code=404)


@app.get("/api/metrics")
async def get_metrics():
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
    summary["providers"] = router.get_provider_statuses()
    return JSONResponse(content=summary)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
