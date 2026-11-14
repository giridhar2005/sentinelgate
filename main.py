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

from cache import SemanticCache
from evals import EvalsManager
from guardrails import JSONSelfHealer, PIIGuardrail
from router import ProviderRouter

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("sentinelgate")

CONFIG_PATH = os.getenv("SENTINELGATE_CONFIG", "config.yaml")
DB_PATH = os.getenv("SENTINELGATE_DB", "sentinelgate.db")

router = ProviderRouter(config_path=CONFIG_PATH)
cache = SemanticCache(db_path=DB_PATH)
evals = EvalsManager(db_path=DB_PATH)


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


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "SentinelGate",
        "version": "0.1.0",
        "timestamp": time.time(),
    }


@app.get("/v1/models")
async def list_models():
    models = router.get_all_models()
    return {"object": "list", "data": models}


@app.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    background_tasks: BackgroundTasks,
    x_no_cache: Optional[str] = Header(None, alias="X-No-Cache"),
):
    start_time = time.time()
    req_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    raw_dict = req.model_dump(exclude_unset=True)

    # 1. PII Masking Guardrail
    masked_messages, pii_mapping = PIIGuardrail.mask_messages(req.messages)
    active_req = dict(raw_dict)
    active_req["messages"] = masked_messages

    requested_model = req.model
    provider, internal_model, _ = router.resolve_model(requested_model)

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

    # 3. Direct REST execution with Key Rotation & Failover
    try:
        upstream_res, chosen_provider, chosen_model = await router.execute_request(
            active_req, stream=bool(req.stream)
        )
    except Exception as e:
        logger.error(f"Execution failed for {requested_model}: {e}")
        raise HTTPException(status_code=502, detail=f"All upstream providers failed: {e}")

    # 4. Handle SSE Streaming Response
    if req.stream:
        async def event_generator():
            try:
                async for chunk in upstream_res:
                    if pii_mapping:
                        chunk = PIIGuardrail.restore_text(chunk, pii_mapping)
                    yield chunk
            except Exception as stream_err:
                logger.error(f"Streaming error: {stream_err}")

        # Stream latency logging (first chunk estimate)
        evals.log_request(
            request_id=req_id,
            model=requested_model,
            provider=chosen_provider,
            latency_ms=(time.time() - start_time) * 1000.0,
            cache_hit=False,
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=0.0,
        )
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


@app.get("/dashboard", response_class=HTMLResponse)
async def serve_dashboard():
    dashboard_file = "dashboard.html"
    if os.path.exists(dashboard_file):
        with open(dashboard_file, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Dashboard file not found</h1>", status_code=404)


@app.get("/api/metrics")
async def get_metrics():
    return JSONResponse(content=evals.get_metrics_summary())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
