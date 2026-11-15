from __future__ import annotations

import logging
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, cast

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from sentinelgate.core.config import AppConfig
from sentinelgate.core.security import make_openai_error
from sentinelgate.core.telemetry_otlp import OTLPExporter, TraceContext
from sentinelgate.services.agent_guard import AgentToolGuard
from sentinelgate.services.budget import BudgetManager, VirtualKeyManager
from sentinelgate.services.cache import SemanticCache
from sentinelgate.services.evals import EvalsManager
from sentinelgate.services.guardrails import (
    PIIGuardrail,
    SecretRedactor,
)
from sentinelgate.services.router import ProviderRouter

logger = logging.getLogger("sentinelgate.pipeline")


class GatewayPipeline:
    """Unified request lifecycle pipeline for /v1/chat/completions and /v1/messages.

    Executes in strictly ordered stages:
    auth -> rate-limit -> guardrails (PII/secrets/injection) -> semantic cache ->
    budget check -> routing/hedging/failover -> provider execution ->
    tool firewall -> streaming redaction -> metrics.
    """

    def __init__(
        self,
        router: ProviderRouter,
        cache: SemanticCache,
        budget: BudgetManager,
        evals: Optional[EvalsManager] = None,
        virtual_keys: Optional[VirtualKeyManager] = None,
        config: Optional[AppConfig] = None,
    ):
        self.router = router
        self.cache = cache
        self.budget = budget
        self.evals = evals
        self.virtual_keys = virtual_keys
        self.config = config
        self.otlp = OTLPExporter()

    @staticmethod
    def extract_user_prompt(messages: List[Dict[str, Any]]) -> str:
        from sentinelgate.services.pipeline_helpers import extract_user_prompt

        return extract_user_prompt(messages)

    @staticmethod
    def extract_system_prompt(messages: List[Dict[str, Any]], system_param: Any = "") -> str:
        from sentinelgate.services.pipeline_helpers import extract_system_prompt

        return extract_system_prompt(messages, system_param)

    async def execute_chat(
        self,
        request: Request,
        messages: List[Dict[str, Any]],
        model: str,
        protocol: str = "openai",  # "openai" or "anthropic"
        system_prompt: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        stream: bool = False,
        bypass_cache: bool = False,
        hedging_requested: bool = False,
        extra_payload: Optional[Dict[str, Any]] = None,
    ) -> Any:
        t_perf_start = time.perf_counter()
        req_id = (
            f"chatcmpl-{uuid.uuid4().hex[:12]}"
            if protocol == "openai"
            else f"msg_{uuid.uuid4().hex[:16]}"
        )

        trace_header = request.headers.get("traceparent")
        trace_ctx = (
            TraceContext.from_header(trace_header)
            if trace_header
            else TraceContext(trace_id=uuid.uuid4().hex, span_id=uuid.uuid4().hex[:16])
        )

        auth_ctx = getattr(request.state, "auth_context", None)
        tenant_id = auth_ctx.token if auth_ctx else "default"
        rate_limit_headers: Dict[str, str] = getattr(request.state, "rate_limit_headers", {})

        user_prompt = self.extract_user_prompt(messages)
        full_system_prompt = self.extract_system_prompt(messages, system_prompt)

        # ---------------------------------------------------------------------
        # STAGE 2: GUARDRAILS (Secrets & Prompt Injection Inspection)
        # ---------------------------------------------------------------------
        guard_cfg = getattr(self.config, "guardrails", None)
        configured_action = getattr(guard_cfg, "secret_action", "redact") if guard_cfg else "redact"
        if (
            guard_cfg
            and getattr(guard_cfg, "block_secrets", False)
            and "secret_action" not in guard_cfg.model_fields_set
        ):
            configured_action = "block"
        secret_action = request.headers.get("X-Secret-Action", configured_action).lower()

        secret_mapping: Dict[str, str] = {}
        from sentinelgate.services.pipeline_guardrails import run_inbound_guardrails

        err_resp, masked_messages, secret_mapping, pii_mapping, scrubbed_count = (
            run_inbound_guardrails(
                messages=messages,
                secret_action=secret_action,
                req_id=req_id,
                evals=self.evals,
                protocol=protocol,
                t_perf_start=t_perf_start,
            )
        )
        if err_resp:
            return err_resp

        # ---------------------------------------------------------------------
        # STAGE 3: SEMANTIC CACHE LOOKUP (Multi-Tenant Isolated)
        # ---------------------------------------------------------------------
        no_cache_hdr = request.headers.get("X-No-Cache") or ("true" if bypass_cache else None)
        cache_ctrl = request.headers.get("Cache-Control")

        if not stream and not (tools and len(tools) > 0):
            cached_result = self.cache.lookup(
                messages=masked_messages,
                model=model,
                tenant_id=tenant_id,
                system_prompt=full_system_prompt,
                tools=tools,
                temperature=temperature,
                bypass_header=no_cache_hdr,
                cache_control=cache_ctrl,
            )
            if cached_result:
                from sentinelgate.services.pipeline_helpers import build_cache_hit_response

                cached_resp, sim_score = cached_result
                return build_cache_hit_response(
                    cached_resp=cached_resp,
                    t_perf_start=t_perf_start,
                    pii_mapping=pii_mapping,
                    secret_mapping=secret_mapping,
                    req_id=req_id,
                    model=model,
                    trace_ctx=trace_ctx,
                    scrubbed_count=scrubbed_count,
                    rate_limit_headers=rate_limit_headers,
                    evals=self.evals,
                    protocol=protocol,
                    user_prompt=user_prompt,
                    to_anthropic_fn=self._to_anthropic_response,
                )

        # ---------------------------------------------------------------------
        # STAGE 4: BUDGET & CIRCUIT BREAKER CHECK
        # ---------------------------------------------------------------------
        estimated_prompt_tokens = max(1, len(user_prompt) // 4)
        is_budget_ok, current_spent, daily_limit = self.budget.check_budget()
        if not is_budget_ok:
            budget_err = f"Daily Budget Limit Exceeded: ${current_spent:.2f} >= ${daily_limit:.2f}"
            if protocol == "anthropic":
                return JSONResponse(
                    status_code=429,
                    content={
                        "type": "error",
                        "error": {
                            "type": "rate_limit_error",
                            "message": budget_err,
                        },
                    },
                    headers=rate_limit_headers,
                )
            return make_openai_error(
                budget_err,
                error_type="insufficient_quota",
                code="budget_limit_exceeded",
                status_code=429,
            )

        # ---------------------------------------------------------------------
        # STAGE 5 & 6: ROUTING / HEDGING & PROVIDER EXECUTION
        # ---------------------------------------------------------------------
        provider, internal_model, _ = self.router.resolve_model(model, masked_messages)

        request_payload: Dict[str, Any] = {
            "model": model,
            "messages": masked_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        if tools:
            request_payload["tools"] = tools
        if extra_payload:
            for k, v in extra_payload.items():
                if k not in request_payload:
                    request_payload[k] = v

        hedge_header = request.headers.get("X-Speculative-Hedge", "").lower() in ("true", "1")
        hedge_body = bool(extra_payload.get("hedge", False)) if extra_payload else False
        enable_hedging = (
            (hedge_header or hedging_requested or hedge_body) and not stream and not tools
        )

        tool_blocked = False
        tool_detail: Optional[str] = None
        hedge_meta = None

        if stream:
            # Streaming execution
            raw_stream, chosen_p, chosen_m = await self.router.execute_request(
                request_payload, stream=True
            )
            return await self._handle_streaming_pipeline(
                raw_gen=cast(AsyncGenerator[str, None], raw_stream),
                protocol=protocol,
                req_id=req_id,
                chosen_p=chosen_p,
                chosen_m=chosen_m,
                user_prompt=user_prompt,
                rate_limit_headers=rate_limit_headers,
                trace_ctx=trace_ctx,
                secret_mapping=secret_mapping,
                model=model,
                scrubbed_count=scrubbed_count,
            )

        # Non-streaming execution
        if enable_hedging:
            raw_res, chosen_p, chosen_m, hedge_meta = await self.router.execute_hedged_request(
                request_payload
            )
        else:
            raw_res, chosen_p, chosen_m = await self.router.execute_request(
                request_payload, stream=False
            )

        lat_ms = (time.perf_counter() - t_perf_start) * 1000.0
        response_json = dict(cast(Dict[str, Any], raw_res))

        # ---------------------------------------------------------------------
        # STAGE 7: AGENT TOOL FIREWALL (Pre-Execution Inspection)
        # ---------------------------------------------------------------------
        is_safe, threat, detail, sanitized = AgentToolGuard.inspect_response(response_json)
        if not is_safe:
            tool_blocked = True
            tool_detail = detail
            response_json = sanitized
            if self.evals:
                self.evals.log_guardrail_event(
                    req_id, "AGENT_TOOL_BLOCKED", f"{threat}: {detail}", blocked=True
                )

        # ---------------------------------------------------------------------
        # STAGE 8: SECRET REDACTION & PII RESTORATION ON RESPONSE
        # ---------------------------------------------------------------------
        if pii_mapping:
            response_json = PIIGuardrail.restore_in_response(response_json, pii_mapping)
        if secret_mapping:
            response_json = SecretRedactor.restore_in_response(response_json, secret_mapping)

        # ---------------------------------------------------------------------
        # STAGE 9: STORE IN CACHE (NEVER cache tool-call responses)
        # ---------------------------------------------------------------------
        usage = response_json.get("usage", {})
        in_tok = usage.get("prompt_tokens", estimated_prompt_tokens)
        out_tok = usage.get("completion_tokens", 10)

        if not tool_blocked and not tools:
            self.cache.store(
                messages=masked_messages,
                model=model,
                response=response_json,
                tenant_id=tenant_id,
                system_prompt=full_system_prompt,
                tools=tools,
                temperature=temperature,
                prompt_tokens=in_tok,
                completion_tokens=out_tok,
            )

        # ---------------------------------------------------------------------
        # STAGE 10: METRICS & HEADERS
        # ---------------------------------------------------------------------
        cost_usd = self.router.get_model_cost(chosen_p, chosen_m, in_tok, out_tok)
        self.budget.record_spend(cost_usd)

        auto_saved_usd = 0.0
        if model == "auto":
            frontier_cost = self.router.get_model_cost("openai", "gpt-4o", in_tok, out_tok)
            auto_saved_usd = max(0.0, frontier_cost - cost_usd)

        from sentinelgate.services.pipeline_helpers import build_upstream_headers

        headers = build_upstream_headers(
            req_id=req_id,
            chosen_p=chosen_p,
            lat_ms=lat_ms,
            tool_blocked=tool_blocked,
            tool_detail=tool_detail,
            trace_ctx=trace_ctx,
            scrubbed_count=scrubbed_count,
            hedge_meta=hedge_meta,
            model=model,
            chosen_m=chosen_m,
            router_instance=self.router,
            rate_limit_headers=rate_limit_headers,
            cost_usd=cost_usd,
            auto_saved_usd=auto_saved_usd,
        )

        if self.evals:
            self.evals.log_request(
                request_id=req_id,
                model=model,
                provider=chosen_p,
                latency_ms=lat_ms,
                cache_hit=False,
                prompt_tokens=in_tok,
                completion_tokens=out_tok,
                cost_usd=cost_usd,
                cost_saved_usd=auto_saved_usd,
                status="BLOCKED" if tool_blocked else "UPSTREAM",
                prompt_snippet=user_prompt[:250],
            )

        if protocol == "anthropic":
            anthropic_json = self._to_anthropic_response(
                response_json, req_id, chosen_m, user_prompt
            )
            return JSONResponse(status_code=200, content=anthropic_json, headers=headers)

        return JSONResponse(status_code=200, content=response_json, headers=headers)

    async def _handle_streaming_pipeline(
        self,
        raw_gen: AsyncGenerator[str, None],
        protocol: str,
        req_id: str,
        chosen_p: str,
        chosen_m: str,
        user_prompt: str,
        rate_limit_headers: Dict[str, str],
        trace_ctx: TraceContext,
        secret_mapping: Dict[str, str],
        model: str = "gpt-4o-mini",
        scrubbed_count: int = 0,
    ) -> StreamingResponse:
        from sentinelgate.services.pipeline_streaming import handle_streaming_pipeline

        return await handle_streaming_pipeline(
            raw_gen=raw_gen,
            protocol=protocol,
            req_id=req_id,
            chosen_p=chosen_p,
            chosen_m=chosen_m,
            user_prompt=user_prompt,
            rate_limit_headers=rate_limit_headers,
            trace_ctx=trace_ctx,
            secret_mapping=secret_mapping,
            router_instance=self.router,
            model=model,
            scrubbed_count=scrubbed_count,
        )

    def _to_anthropic_response(
        self,
        openai_json: Dict[str, Any],
        req_id: str,
        model: str,
        user_prompt: str,
    ) -> Dict[str, Any]:
        from sentinelgate.services.pipeline_streaming import convert_openai_to_anthropic

        return convert_openai_to_anthropic(
            openai_json=openai_json,
            req_id=req_id,
            model=model,
            user_prompt=user_prompt,
        )
