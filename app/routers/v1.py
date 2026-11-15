from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, BackgroundTasks, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app.core.security import make_openai_error, parse_bearer_token
from app.core.telemetry_otlp import OTLPExporter, OTLPSpan, TraceContext
from app.services.agent_guard import AgentToolGuard
from app.services.guardrails import (
    ContextCompactor,
    JSONSelfHealer,
    PIIGuardrail,
    PromptOptimizer,
    SecretRedactor,
    SecurityInspector,
)
from app.services.streaming_redactor import sse_sliding_window_redactor

logger = logging.getLogger("sentinelgate.v1")
otlp_exporter = OTLPExporter()

router = APIRouter(prefix="/v1", tags=["OpenAI Compatible v1"])


class ChatCompletionRequest(BaseModel):
    model: str = "gpt-4o-mini"
    messages: List[Dict[str, Any]]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False
    response_format: Optional[Dict[str, Any]] = None
    hedge: Optional[bool] = False
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None


class EmbeddingsRequest(BaseModel):
    model: str = "text-embedding-3-small"
    input: Union[str, List[str]]
    user: Optional[str] = None


class CreateKeyRequest(BaseModel):
    name: str
    daily_budget_usd: Optional[float] = 10.0
    monthly_budget_usd: Optional[float] = 300.0
    rate_limit_rpm: Optional[int] = 60
    rate_limit_tpm: Optional[int] = 60000


class CacheWarmItem(BaseModel):
    prompt: str
    response: str
    model: Optional[str] = "gpt-4o-mini"


@router.get("/models")
async def list_models(request: Request):
    gateway_router = request.app.state.router
    return JSONResponse(content={"object": "list", "data": gateway_router.get_all_models()})


@router.post("/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_no_cache: Optional[str] = Header(None, alias="X-No-Cache"),
    cache_control: Optional[str] = Header(None, alias="Cache-Control"),
    x_hedge: Optional[str] = Header(None, alias="X-Hedge"),
    traceparent: Optional[str] = Header(None, alias="traceparent"),
):
    start_time = time.time()
    t_perf_start = time.perf_counter()
    trace_ctx = TraceContext.from_header(traceparent)
    req_id = f"chatcmpl-{trace_ctx.span_id}"
    user_prompt = (
        " ".join([str(m.get("content", "")) for m in req.messages if m.get("role") == "user"])
        or "User prompt"
    )

    t_start_ns = int(time.time() * 1e9)
    root_span = OTLPSpan(
        name="sentinelgate.request",
        trace_id=trace_ctx.trace_id,
        span_id=trace_ctx.span_id,
        parent_span_id=trace_ctx.parent_span_id,
        start_time_ns=t_start_ns,
        attributes={
            "http.method": "POST",
            "http.route": "/v1/chat/completions",
            "llm.request.model": req.model,
        },
    )

    # Access app services
    app_router = request.app.state.router
    app_cache = request.app.state.cache
    app_evals = request.app.state.evals
    app_budget = request.app.state.budget
    app_keys = request.app.state.virtual_keys

    # 1. Virtual Key Authentication & Dual-Bucket Rate Limiting (RPM + TPM)
    rate_limit_headers: Dict[str, str] = {}
    estimated_prompt_tokens = max(1, len(user_prompt) // 4)
    bearer_token = parse_bearer_token(authorization)
    if bearer_token and bearer_token.startswith("sg-"):
        is_valid, err_msg, rl_hdrs = app_keys.verify_and_rate_limit(
            bearer_token, estimated_tokens=estimated_prompt_tokens
        )
        rate_limit_headers = rl_hdrs
        if not is_valid:
            status_code = 429 if "rate limit" in (err_msg or "").lower() else 401
            code = "rate_limit_exceeded" if status_code == 429 else "invalid_api_key"
            err_resp = make_openai_error(
                err_msg or "Authentication failed",
                error_type="auth_error",
                code=code,
                status_code=status_code,
            )
            for k, v in rate_limit_headers.items():
                err_resp.headers[k] = v
            return err_resp

    # 2. Pre-Flight Guardrails
    t_guard_start = time.perf_counter()

    # Secret Redaction / Blocking Firewall
    guard_cfg = getattr(request.app.state.config, "guardrails", None)
    configured_action = getattr(guard_cfg, "secret_action", "redact") if guard_cfg else "redact"
    if (
        guard_cfg
        and getattr(guard_cfg, "block_secrets", False)
        and "secret_action" not in guard_cfg.model_fields_set
    ):
        configured_action = "block"
    secret_action = request.headers.get("X-Secret-Action", configured_action).lower()

    secret_mapping: Dict[str, str] = {}
    scrubbed_count = 0

    if secret_action == "block":
        is_secrets_safe, secret_threat = SecurityInspector.inspect_secrets(req.messages)
        if not is_secrets_safe:
            lat_ms = (time.perf_counter() - t_perf_start) * 1000.0
            guard_ms = (time.perf_counter() - t_guard_start) * 1000.0
            app_evals.log_guardrail_event(req_id, "SECRET_LEAK", secret_threat or "", blocked=True)
            app_evals.log_request(
                request_id=req_id,
                model=req.model,
                provider="guardrail",
                latency_ms=lat_ms,
                cache_hit=False,
                prompt_tokens=len(user_prompt) // 4,
                completion_tokens=0,
                cost_usd=0.0,
                status="BLOCKED",
                prompt_snippet=user_prompt[:250],
                similarity_score=0.0,
                timing_breakdown={"guardrail_ms": round(guard_ms, 2), "total_ms": round(lat_ms, 2)},
                response_snippet=f"Security Violation: {secret_threat}",
            )
            return make_openai_error(
                f"Security Violation: {secret_threat}",
                error_type="security_guardrail_violation",
                code="secret_leak_detected",
                status_code=400,
            )
    elif secret_action == "redact":
        sanitized_messages, secret_mapping, scrubbed_count = SecretRedactor.redact_messages(
            req.messages
        )
        if scrubbed_count > 0:
            app_evals.log_guardrail_event(
                req_id,
                "SECRET_REDACTED",
                f"Scrubbed {scrubbed_count} secret credential(s) or URI(s)",
                blocked=False,
            )
            req.messages = sanitized_messages

    guard_ms = (time.perf_counter() - t_guard_start) * 1000.0

    # B. Prompt Injection & Jailbreak Heuristic
    is_safe, threat_reason = SecurityInspector.inspect_injection(req.messages)
    if not is_safe:
        lat_ms = (time.perf_counter() - t_perf_start) * 1000.0
        app_evals.log_guardrail_event(req_id, "PROMPT_INJECTION", threat_reason or "", blocked=True)
        app_evals.log_request(
            request_id=req_id,
            model=req.model,
            provider="guardrail",
            latency_ms=lat_ms,
            cache_hit=False,
            prompt_tokens=len(user_prompt) // 4,
            completion_tokens=0,
            cost_usd=0.0,
            status="BLOCKED",
            prompt_snippet=user_prompt[:250],
            similarity_score=0.0,
            timing_breakdown={"guardrail_ms": round(guard_ms, 2), "total_ms": round(lat_ms, 2)},
            response_snippet=f"Prompt Security Violation: {threat_reason}",
        )
        return make_openai_error(
            f"Prompt Security Violation: {threat_reason}",
            error_type="invalid_request_error",
            code="prompt_injection_detected",
            status_code=400,
        )

    # 3. Context Compaction & Whitespace Token Optimization
    compacted = ContextCompactor.compact(req.messages)
    optimized_messages = PromptOptimizer.optimize_messages(compacted)

    # 4. PII Masking
    masked_messages, pii_mapping = PIIGuardrail.mask_messages(optimized_messages)
    raw_dict = req.model_dump(exclude_unset=True)
    active_req = dict(raw_dict)
    active_req["messages"] = masked_messages
    active_req["_headers"] = {"traceparent": trace_ctx.create_child().to_header()}

    requested_model = req.model
    provider, internal_model, _ = app_router.resolve_model(requested_model, masked_messages)

    # 5. Semantic Cache Lookup (Non-Streaming)
    cache_lookup_ms = 0.0
    if not req.stream:
        t_cache_start = time.perf_counter()
        cached_result = app_cache.lookup(
            messages=masked_messages,
            model=requested_model,
            bypass_header=x_no_cache,
            cache_control=cache_control,
        )
        cache_lookup_ms = (time.perf_counter() - t_cache_start) * 1000.0
        if cached_result:
            cached_resp, sim_score = cached_result
            latency_ms = (time.time() - start_time) * 1000.0

            final_resp = PIIGuardrail.restore_in_response(cached_resp, pii_mapping)
            if secret_mapping:
                final_resp = SecretRedactor.restore_in_response(final_resp, secret_mapping)
            final_resp["id"] = req_id

            usage = final_resp.get("usage", {})
            in_tokens = usage.get("prompt_tokens", 0)
            out_tokens = usage.get("completion_tokens", 0)
            cost_saved = app_router.get_model_cost(provider, internal_model, in_tokens, out_tokens)

            resp_snippet = (
                final_resp.get("choices", [{}])[0].get("message", {}).get("content", "")[:300]
            )
            timing = {
                "guardrail_ms": round(guard_ms, 2),
                "embedding_ms": round(cache_lookup_ms * 0.7, 2),
                "cache_lookup_ms": round(cache_lookup_ms * 0.3, 2),
                "upstream_ms": 0.0,
                "total_ms": round(latency_ms, 2),
            }

            app_evals.log_request(
                request_id=req_id,
                model=requested_model,
                provider=f"{provider}(cached)",
                latency_ms=latency_ms,
                cache_hit=True,
                prompt_tokens=in_tokens,
                completion_tokens=out_tokens,
                cost_usd=0.0,
                cost_saved_usd=cost_saved,
                status="CACHE_HIT",
                prompt_snippet=user_prompt[:250],
                similarity_score=round(float(sim_score), 4),
                timing_breakdown=timing,
                response_snippet=resp_snippet,
            )

            headers = {
                "X-Request-ID": req_id,
                "X-Cache": "HIT",
                "X-Provider-Used": f"{provider}(cached)",
                "X-Latency-Ms": f"{latency_ms:.2f}",
                "X-Agent-Firewall-Blocked": "false",
                "traceparent": trace_ctx.to_header(),
            }
            if scrubbed_count > 0:
                headers["X-Secrets-Scrubbed"] = str(scrubbed_count)
            headers.update(rate_limit_headers)
            if bearer_token and bearer_token.startswith("sg-"):
                app_keys.record_token_adjustment(
                    bearer_token, (in_tokens + out_tokens) - estimated_prompt_tokens
                )
            root_span.attributes.update({
                "cache.hit": True,
                "llm.provider": provider,
                "llm.cost_usd": 0.0,
                "llm.cost_saved_usd": cost_saved,
                "llm.usage.prompt_tokens": in_tokens,
                "llm.usage.completion_tokens": out_tokens,
            })
            root_span.finish(status_code=1)
            otlp_exporter.record_span(root_span)
            return JSONResponse(content=final_resp, headers=headers)

    # 6. Daily Budget Circuit Breaker Check
    is_budget_ok, spent_today, budget_limit = app_budget.check_budget()
    if not is_budget_ok:
        return make_openai_error(
            f"Daily Budget Limit Exceeded: Spent ${spent_today:.4f} of ${budget_limit:.2f} limit today.",
            error_type="budget_limit_exceeded",
            code="budget_limit_exceeded",
            status_code=429,
        )

    # 7. Upstream Direct REST Execution
    hedge_meta: Dict[str, Any] = {}
    try:
        use_hedge = (req.hedge or (x_hedge and x_hedge.lower() == "true")) and not req.stream
        if use_hedge:
            res_tuple = await app_router.execute_hedged_request(active_req)
            if len(res_tuple) == 4:
                upstream_res, chosen_provider, chosen_model, hedge_meta = res_tuple
            else:
                upstream_res, chosen_provider, chosen_model = res_tuple[:3]
        else:
            upstream_res, chosen_provider, chosen_model = await app_router.execute_request(
                active_req, stream=bool(req.stream)
            )
    except Exception as e:
        logger.error(f"Execution failed for {requested_model}: {e}")
        return make_openai_error(
            "All upstream providers failed or timed out", error_type="upstream_error", status_code=502
        )

    # 8. Handle SSE Streaming Response
    if req.stream:
        ttft_logged = {"done": False}
        redacted_stream = sse_sliding_window_redactor(
            upstream_res,
            secret_mapping=secret_mapping,
            action=secret_action,
        )

        async def event_generator():
            try:
                async for chunk in redacted_stream:
                    if not ttft_logged["done"]:
                        ttft_ms = (time.time() - start_time) * 1000.0
                        stream_status = (
                            "FAILOVER"
                            if chosen_provider != provider
                            else ("REDACTED" if scrubbed_count > 0 else "UPSTREAM")
                        )
                        app_evals.log_request(
                            request_id=req_id,
                            model=requested_model,
                            provider=chosen_provider,
                            latency_ms=ttft_ms,
                            cache_hit=False,
                            prompt_tokens=0,
                            completion_tokens=0,
                            cost_usd=0.0,
                            ttft_ms=ttft_ms,
                            status=stream_status,
                            prompt_snippet=user_prompt[:250],
                        )
                        root_span.attributes.update({
                            "cache.hit": False,
                            "llm.provider": chosen_provider,
                            "llm.ttft_ms": ttft_ms,
                            "stream": True,
                        })
                        root_span.finish(status_code=1)
                        otlp_exporter.record_span(root_span)
                        ttft_logged["done"] = True
                    if pii_mapping:
                        chunk = PIIGuardrail.restore_text(chunk, pii_mapping)
                    yield chunk
            except Exception as stream_err:
                logger.error(f"Streaming error: {stream_err}")

        stream_headers = {
            "X-Request-ID": req_id,
            "X-Cache": "MISS",
            "X-Provider-Used": chosen_provider,
            "X-Latency-Ms": f"{(time.time() - start_time) * 1000.0:.2f}",
            "X-Agent-Firewall-Blocked": "false",
            "traceparent": trace_ctx.to_header(),
        }
        if scrubbed_count > 0:
            stream_headers["X-Secrets-Scrubbed"] = str(scrubbed_count)
        stream_headers.update(rate_limit_headers)
        if requested_model == "auto":
            auto_meta = getattr(app_router, "last_routing_metadata", {})
            stream_headers["X-Auto-Route"] = f"{chosen_provider}/{chosen_model}"
            if auto_meta:
                stream_headers["X-Auto-Complexity"] = (
                    f"{auto_meta.get('classification', 'simple')} ({auto_meta.get('score', 0)}/100)"
                )
        return StreamingResponse(
            event_generator(), media_type="text/event-stream", headers=stream_headers
        )

    # 9. Non-Streaming Post-Processing
    if JSONSelfHealer.is_json_requested(active_req):
        upstream_res = await JSONSelfHealer.repair_response(
            router=app_router,
            request_data=active_req,
            initial_response=upstream_res,
        )

    final_resp = PIIGuardrail.restore_in_response(upstream_res, pii_mapping)
    if secret_mapping:
        final_resp = SecretRedactor.restore_in_response(final_resp, secret_mapping)
    final_resp["id"] = req_id

    # Agent Tool-Call Firewall Inspection
    is_tool_safe, tool_threat, tool_detail, sanitized_resp = AgentToolGuard.inspect_response(final_resp)
    tool_blocked = False
    if not is_tool_safe:
        tool_blocked = True
        app_evals.log_guardrail_event(
            request_id=req_id,
            event_type="TOOL_CALL_BLOCKED",
            threat_detail=tool_detail or "Blocked by Agent Tool Guard",
            blocked=True,
        )
        final_resp = sanitized_resp

    latency_ms = (time.time() - start_time) * 1000.0
    usage = final_resp.get("usage", {})
    in_tok = usage.get("prompt_tokens", max(1, len(str(req.messages)) // 4))
    out_tok = usage.get(
        "completion_tokens",
        max(1, len(final_resp.get("choices", [{}])[0].get("message", {}).get("content", "")) // 4),
    )

    cost_usd = app_router.get_model_cost(chosen_provider, chosen_model, in_tok, out_tok)

    # Calculate exact USD savings vs frontier pricing when model="auto" routes to a cheaper model
    auto_saved_usd = 0.0
    if requested_model == "auto":
        frontier_cost = app_router.get_model_cost("openai", "gpt-4o", in_tok, out_tok)
        auto_saved_usd = max(0.0, frontier_cost - cost_usd)

    # Store in Semantic Cache only if tool call was safe
    if not tool_blocked:
        app_cache.store(
            messages=masked_messages,
            model=requested_model,
            response=final_resp,
            prompt_tokens=in_tok,
            completion_tokens=out_tok,
        )

    if tool_blocked:
        status = "BLOCKED"
    elif chosen_provider != provider:
        status = "FAILOVER"
    elif scrubbed_count > 0:
        status = "REDACTED"
    else:
        status = "UPSTREAM"
    resp_snippet = final_resp.get("choices", [{}])[0].get("message", {}).get("content", "")[:300]
    timing = {
        "guardrail_ms": round(guard_ms, 2),
        "embedding_ms": round(cache_lookup_ms * 0.7, 2),
        "cache_lookup_ms": round(cache_lookup_ms * 0.3, 2),
        "upstream_ms": round(max(0.0, latency_ms - guard_ms - cache_lookup_ms), 2),
        "total_ms": round(latency_ms, 2),
    }

    # Log metrics & spend
    app_evals.log_request(
        request_id=req_id,
        model=requested_model,
        provider=chosen_provider,
        latency_ms=latency_ms,
        cache_hit=False,
        prompt_tokens=in_tok,
        completion_tokens=out_tok,
        cost_usd=cost_usd,
        cost_saved_usd=auto_saved_usd,
        status=status,
        prompt_snippet=user_prompt[:250],
        similarity_score=0.0,
        timing_breakdown=timing,
        response_snippet=resp_snippet,
    )
    app_budget.record_spend(cost_usd)

    # Sample 10% Shadow Parity Evals
    if app_evals.should_sample_shadow(cache_hit=False):
        resp_text = final_resp.get("choices", [{}])[0].get("message", {}).get("content", "")
        background_tasks.add_task(
            app_evals.run_shadow_eval,
            router=app_router,
            request_id=req_id,
            original_model=requested_model,
            messages=masked_messages,
            original_response_text=resp_text,
        )

    headers = {
        "X-Request-ID": req_id,
        "X-Cache": "MISS",
        "X-Provider-Used": chosen_provider,
        "X-Latency-Ms": f"{latency_ms:.2f}",
        "X-Agent-Firewall-Blocked": "true" if tool_blocked else "false",
        "traceparent": trace_ctx.to_header(),
    }
    if tool_blocked and tool_threat:
        headers["X-Agent-Firewall-Reason"] = str(tool_threat)
    if scrubbed_count > 0:
        headers["X-Secrets-Scrubbed"] = str(scrubbed_count)
    if hedge_meta:
        headers["X-Hedged-Request"] = "true"
        headers["X-Hedge-Winner"] = str(hedge_meta.get("winner", "primary"))
        headers["X-Hedge-Triggered"] = "true" if hedge_meta.get("hedged_triggered") else "false"
    headers.update(rate_limit_headers)
    if bearer_token and bearer_token.startswith("sg-"):
        app_keys.record_token_adjustment(bearer_token, (in_tok + out_tok) - estimated_prompt_tokens)
    if requested_model == "auto":
        headers["X-Auto-Route"] = f"{chosen_provider}/{chosen_model}"
        headers["X-Auto-Saved-USD"] = f"{auto_saved_usd:.6f}"
        auto_meta = getattr(app_router, "last_routing_metadata", {})
        if auto_meta:
            headers["X-Auto-Complexity"] = (
                f"{auto_meta.get('classification', 'simple')} ({auto_meta.get('score', 0)}/100)"
            )

    root_span.attributes.update({
        "cache.hit": False,
        "llm.provider": chosen_provider,
        "llm.response.model": chosen_model,
        "llm.cost_usd": cost_usd,
        "llm.cost_saved_usd": auto_saved_usd,
        "llm.usage.prompt_tokens": in_tok,
        "llm.usage.completion_tokens": out_tok,
        "agent.tool_blocked": tool_blocked,
    })
    root_span.finish(status_code=2 if tool_blocked else 1)
    otlp_exporter.record_span(root_span)

    return JSONResponse(content=final_resp, headers=headers)


@router.post("/embeddings")
async def create_embeddings(req: EmbeddingsRequest, request: Request):
    start_time = time.time()
    req_id = f"emb-{uuid.uuid4().hex[:12]}"
    app_router = request.app.state.router

    try:
        emb_response = await app_router.execute_embeddings(model=req.model, inputs=req.input)
        latency_ms = (time.time() - start_time) * 1000.0
        headers = {
            "X-Request-ID": req_id,
            "X-Provider-Used": "sentinel-fastembed",
            "X-Latency-Ms": f"{latency_ms:.2f}",
        }
        return JSONResponse(content=emb_response, headers=headers)
    except Exception as e:
        logger.error(f"Embeddings creation failed: {e}")
        return make_openai_error("Embedding generation failed.", status_code=500)


@router.post("/keys")
async def create_key(req: CreateKeyRequest, request: Request):
    app_keys = request.app.state.virtual_keys
    new_k = app_keys.create_key(
        name=req.name,
        daily_budget_usd=req.daily_budget_usd or 10.0,
        monthly_budget_usd=req.monthly_budget_usd or 300.0,
        rate_limit_rpm=req.rate_limit_rpm or 60,
        rate_limit_tpm=req.rate_limit_tpm or 60000,
    )
    return JSONResponse(content=new_k)


@router.get("/keys")
async def list_keys(request: Request):
    app_keys = request.app.state.virtual_keys
    return JSONResponse(content={"keys": app_keys.list_keys()})


@router.delete("/keys/{key_val}")
async def revoke_key(key_val: str, request: Request):
    app_keys = request.app.state.virtual_keys
    success = app_keys.revoke_key(key_val)
    if not success:
        return JSONResponse(status_code=404, content={"error": "Key not found"})
    return JSONResponse(content={"status": "revoked", "key": key_val})


@router.post("/cache/warm")
async def warm_cache(items: List[CacheWarmItem], request: Request):
    app_cache = request.app.state.cache
    for item in items:
        app_cache.store(
            messages=[{"role": "user", "content": item.prompt}],
            model=item.model or "gpt-4o-mini",
            response={
                "id": f"chatcmpl-warm-{uuid.uuid4().hex[:8]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": item.model or "gpt-4o-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": item.response},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": len(item.prompt) // 4,
                    "completion_tokens": len(item.response) // 4,
                    "total_tokens": (len(item.prompt) + len(item.response)) // 4,
                },
            },
        )
    return JSONResponse(content={"status": "success", "prewarmed_count": len(items)})


@router.get("/evals/export")
async def export_evals(request: Request, limit: Optional[int] = 100):
    app_evals = request.app.state.evals
    return JSONResponse(content={"dataset": app_evals.export_golden_dataset(limit=limit or 100)})
