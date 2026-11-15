from __future__ import annotations

import json
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
    ContextCompactor,
    PIIGuardrail,
    PromptOptimizer,
    SecretRedactor,
    SecurityInspector,
)
from sentinelgate.services.router import ProviderRouter
from sentinelgate.services.streaming_redactor import SlidingWindowStreamRedactor

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
        for m in reversed(messages):
            if m.get("role") == "user":
                content = m.get("content", "")
                if isinstance(content, str):
                    return content
                elif isinstance(content, list):
                    return "".join(
                        b.get("text", "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
        return ""

    @staticmethod
    def extract_system_prompt(messages: List[Dict[str, Any]], system_param: Any = "") -> str:
        sys_parts = []
        if system_param:
            if isinstance(system_param, str):
                sys_parts.append(system_param)
            elif isinstance(system_param, list):
                for b in system_param:
                    if isinstance(b, dict) and b.get("type") == "text":
                        sys_parts.append(b.get("text", ""))
        for m in messages:
            if m.get("role") == "system":
                c = m.get("content", "")
                if isinstance(c, str):
                    sys_parts.append(c)
        return " ".join(sys_parts).strip()

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
        scrubbed_count = 0

        if secret_action == "block":
            is_secrets_safe, secret_threat = SecurityInspector.inspect_secrets(messages)
            if not is_secrets_safe:
                lat_ms = (time.perf_counter() - t_perf_start) * 1000.0
                if self.evals:
                    self.evals.log_guardrail_event(
                        req_id, "SECRET_LEAK", secret_threat or "", blocked=True
                    )
                if protocol == "anthropic":
                    return JSONResponse(
                        status_code=400,
                        content={
                            "type": "error",
                            "error": {
                                "type": "invalid_request_error",
                                "message": f"Security Violation: {secret_threat}",
                            },
                        },
                    )
                return make_openai_error(
                    f"Security Violation: {secret_threat}",
                    error_type="security_guardrail_violation",
                    code="secret_leak_detected",
                    status_code=400,
                )
        elif secret_action == "redact":
            messages, secret_mapping, scrubbed_count = SecretRedactor.redact_messages(messages)
            if scrubbed_count > 0 and self.evals:
                self.evals.log_guardrail_event(
                    req_id,
                    "SECRET_REDACTED",
                    f"Scrubbed {scrubbed_count} secret credential(s)",
                    blocked=False,
                )

        # Prompt injection inspection
        is_inj_safe, inj_threat = SecurityInspector.inspect_injection(messages)
        if not is_inj_safe:
            lat_ms = (time.perf_counter() - t_perf_start) * 1000.0
            if self.evals:
                self.evals.log_guardrail_event(
                    req_id, "PROMPT_INJECTION", inj_threat or "", blocked=True
                )
            if protocol == "anthropic":
                return JSONResponse(
                    status_code=400,
                    content={
                        "type": "error",
                        "error": {
                            "type": "invalid_request_error",
                            "message": f"Prompt Security Violation: {inj_threat}",
                        },
                    },
                )
            return make_openai_error(
                f"Prompt Security Violation: {inj_threat}",
                error_type="invalid_request_error",
                code="prompt_injection_detected",
                status_code=400,
            )

        # Context compaction & optimization
        messages = ContextCompactor.compact(messages)
        messages = PromptOptimizer.optimize_messages(messages)

        # PII Masking
        masked_messages, pii_mapping = PIIGuardrail.mask_messages(messages)

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
                cached_resp, sim_score = cached_result
                lat_ms = (time.perf_counter() - t_perf_start) * 1000.0
                resp_payload = dict(cached_resp)

                if pii_mapping:
                    resp_payload = PIIGuardrail.restore_in_response(resp_payload, pii_mapping)
                if secret_mapping:
                    resp_payload = SecretRedactor.restore_in_response(resp_payload, secret_mapping)

                hdrs = {
                    "X-Request-ID": req_id,
                    "X-Cache": "HIT",
                    "X-Provider-Used": f"{resp_payload.get('model', model)}(cached)",
                    "X-Latency-Ms": f"{lat_ms:.2f}",
                    "X-Agent-Firewall-Blocked": "false",
                    "traceparent": trace_ctx.to_header(),
                }
                if scrubbed_count > 0:
                    hdrs["X-Secrets-Scrubbed"] = str(scrubbed_count)
                hdrs.update(rate_limit_headers)

                if self.evals:
                    usage = resp_payload.get("usage", {})
                    in_tok = usage.get("prompt_tokens", len(user_prompt) // 4)
                    out_tok = usage.get("completion_tokens", 10)
                    self.evals.log_request(
                        request_id=req_id,
                        model=model,
                        provider=f"{resp_payload.get('model', model)}(cached)",
                        latency_ms=lat_ms,
                        cache_hit=True,
                        prompt_tokens=in_tok,
                        completion_tokens=out_tok,
                        cost_usd=0.0,
                        cost_saved_usd=0.001,
                        status="CACHE",
                        prompt_snippet=user_prompt[:250],
                    )

                if protocol == "anthropic":
                    anthropic_json = self._to_anthropic_response(
                        resp_payload, req_id, model, user_prompt
                    )
                    return JSONResponse(status_code=200, content=anthropic_json, headers=hdrs)

                return JSONResponse(status_code=200, content=resp_payload, headers=hdrs)

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
        headers = {
            "X-Request-ID": req_id,
            "X-Cache": "MISS",
            "X-Provider-Used": chosen_p,
            "X-Latency-Ms": f"{lat_ms:.2f}",
            "X-Agent-Firewall-Blocked": "true" if tool_blocked else "false",
            "traceparent": trace_ctx.to_header(),
        }
        if tool_blocked and tool_detail:
            headers["X-Agent-Firewall-Reason"] = str(tool_detail)
        if scrubbed_count > 0:
            headers["X-Secrets-Scrubbed"] = str(scrubbed_count)
        if hedge_meta:
            headers["X-Hedged-Request"] = "true"
            headers["X-Hedge-Winner"] = str(hedge_meta.get("winner", "primary"))
            headers["X-Hedge-Triggered"] = "true" if hedge_meta.get("hedged_triggered") else "false"

        cost_usd = self.router.get_model_cost(chosen_p, chosen_m, in_tok, out_tok)
        self.budget.record_spend(cost_usd)

        auto_saved_usd = 0.0
        if model == "auto":
            frontier_cost = self.router.get_model_cost("openai", "gpt-4o", in_tok, out_tok)
            auto_saved_usd = max(0.0, frontier_cost - cost_usd)
            headers["X-Auto-Route"] = f"{chosen_p}/{chosen_m}"
            headers["X-Auto-Saved-USD"] = f"{auto_saved_usd:.6f}"
            auto_meta = getattr(self.router, "last_routing_metadata", {})
            if auto_meta:
                headers["X-Auto-Complexity"] = (
                    f"{auto_meta.get('classification', 'simple')} ({auto_meta.get('score', 0)}/100)"
                )

        headers.update(rate_limit_headers)

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
        """Stream processor with sliding-window secret redactor and tool-call firewall accumulation."""
        headers = {
            "X-Request-ID": req_id,
            "X-Cache": "MISS",
            "X-Provider-Used": chosen_p,
            "traceparent": trace_ctx.to_header(),
        }
        if scrubbed_count > 0:
            headers["X-Secrets-Scrubbed"] = str(scrubbed_count)
        if model == "auto":
            auto_meta = getattr(self.router, "last_routing_metadata", {})
            headers["X-Auto-Route"] = f"{chosen_p}/{chosen_m}"
            if auto_meta:
                headers["X-Auto-Complexity"] = (
                    f"{auto_meta.get('classification', 'simple')} ({auto_meta.get('score', 0)}/100)"
                )
        headers.update(rate_limit_headers)

        # Buffer initial chunks to detect if the stream is starting with tool_calls.
        # This allows inspecting malicious tool calls BEFORE sending the HTTP response headers.
        buffered_chunks: List[str] = []
        is_tool_stream = False
        accumulated_tool_calls: Dict[int, Dict[str, Any]] = {}
        tool_blocked = False
        tool_threat_msg = ""

        try:
            async for raw_line in raw_gen:
                buffered_chunks.append(raw_line)
                if not raw_line or not raw_line.startswith("data: "):
                    continue
                data_str = raw_line[6:].strip()
                if data_str == "[DONE]":
                    break

                try:
                    chunk = json.loads(data_str)
                except Exception:
                    continue

                choices = chunk.get("choices", [])
                if not choices:
                    continue

                delta = choices[0].get("delta", {})
                tool_deltas = delta.get("tool_calls")

                if tool_deltas:
                    is_tool_stream = True
                    for td in tool_deltas:
                        idx = td.get("index", 0)
                        if idx not in accumulated_tool_calls:
                            accumulated_tool_calls[idx] = {
                                "id": td.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                                "name": td.get("function", {}).get("name", ""),
                                "args": [],
                            }
                        fn = td.get("function", {})
                        if fn.get("name"):
                            accumulated_tool_calls[idx]["name"] = fn["name"]
                        if fn.get("arguments"):
                            accumulated_tool_calls[idx]["args"].append(fn["arguments"])
                elif delta.get("content") and not is_tool_stream:
                    # Text tokens started, break peek loop
                    break
        except Exception as e:
            logger.warning(f"Error during stream peeking: {e}")

        # If tool calls were detected, inspect them
        if is_tool_stream and accumulated_tool_calls:
            for idx, t_data in accumulated_tool_calls.items():
                name = t_data.get("name", "")
                full_args = "".join(t_data.get("args", []))
                is_safe, threat, detail = AgentToolGuard.inspect_tool_call(name, full_args)
                if not is_safe:
                    tool_blocked = True
                    tool_threat_msg = str(threat)
                    break

        if tool_blocked:
            headers["X-Agent-Firewall-Blocked"] = "true"
            headers["X-Agent-Firewall-Reason"] = tool_threat_msg
        else:
            headers["X-Agent-Firewall-Blocked"] = "false"

        async def stream_generator():
            redactor = SlidingWindowStreamRedactor(secret_mapping=secret_mapping)

            if tool_blocked:
                refusal_msg = f"SentinelGate Agent Security Firewall blocked tool execution: {tool_threat_msg}."
                if protocol == "openai":
                    refusal_chunk = {
                        "id": req_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": chosen_m,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": refusal_msg},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                    yield f"data: {json.dumps(refusal_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                elif protocol == "anthropic":
                    yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
                    yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': refusal_msg}})}\n\n"
                    yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
                    yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': 20}})}\n\n"
                    yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"
                return

            if is_tool_stream:
                if protocol == "openai":
                    for line in buffered_chunks:
                        yield f"{line}\n\n"
                    # Process any remaining lines from raw_gen
                    async for remaining_line in raw_gen:
                        if remaining_line:
                            yield f"{remaining_line}\n\n"
                elif protocol == "anthropic":
                    for idx, t_data in accumulated_tool_calls.items():
                        full_args = "".join(t_data.get("args", []))
                        yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': idx, 'content_block': {'type': 'tool_use', 'id': t_data['id'], 'name': t_data['name'], 'input': {}}})}\n\n"
                        yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': idx, 'delta': {'type': 'input_json_delta', 'partial_json': full_args}})}\n\n"
                        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': idx})}\n\n"
                    yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'tool_use', 'stop_sequence': None}, 'usage': {'output_tokens': 25}})}\n\n"
                    yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"
                return

            # Pure text stream
            all_lines = buffered_chunks
            for raw_line in all_lines:
                if not raw_line or not raw_line.startswith("data: "):
                    continue
                data_str = raw_line[6:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except Exception:
                    continue
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                content_text = delta.get("content", "")
                if content_text:
                    scrubbed = redactor.process_delta(content_text, is_final=False)
                    if scrubbed:
                        if protocol == "openai":
                            c = dict(chunk)
                            c["choices"][0]["delta"]["content"] = scrubbed
                            yield f"data: {json.dumps(c)}\n\n"
                        elif protocol == "anthropic":
                            yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': scrubbed}})}\n\n"

            async for raw_line in raw_gen:
                if not raw_line or not raw_line.startswith("data: "):
                    continue
                data_str = raw_line[6:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except Exception:
                    continue
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                content_text = delta.get("content", "")
                if content_text:
                    scrubbed = redactor.process_delta(content_text, is_final=False)
                    if scrubbed:
                        if protocol == "openai":
                            c = dict(chunk)
                            c["choices"][0]["delta"]["content"] = scrubbed
                            yield f"data: {json.dumps(c)}\n\n"
                        elif protocol == "anthropic":
                            yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': scrubbed}})}\n\n"

            flush_tail = redactor.process_delta("", is_final=True)
            if flush_tail:
                if protocol == "openai":
                    tail_chunk = {
                        "id": req_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": chosen_m,
                        "choices": [
                            {"index": 0, "delta": {"content": flush_tail}, "finish_reason": None}
                        ],
                    }
                    yield f"data: {json.dumps(tail_chunk)}\n\n"
                elif protocol == "anthropic":
                    yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': flush_tail}})}\n\n"

            if protocol == "openai":
                yield "data: [DONE]\n\n"
            elif protocol == "anthropic":
                yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
                yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': 15}})}\n\n"
                yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"

        if protocol == "anthropic":

            async def anthropic_outer():
                yield f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'id': req_id, 'type': 'message', 'role': 'assistant', 'model': chosen_m, 'content': [], 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': max(1, len(user_prompt) // 4), 'output_tokens': 0, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0}}})}\n\n"
                yield f"event: ping\ndata: {json.dumps({'type': 'ping'})}\n\n"
                if not is_tool_stream:
                    yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
                async for item in stream_generator():
                    yield item

            return StreamingResponse(
                anthropic_outer(), media_type="text/event-stream", headers=headers
            )

        return StreamingResponse(
            stream_generator(), media_type="text/event-stream", headers=headers
        )

    def _to_anthropic_response(
        self,
        openai_json: Dict[str, Any],
        req_id: str,
        model: str,
        user_prompt: str,
    ) -> Dict[str, Any]:
        """Convert standard OpenAI response schema to Anthropic Messages API schema."""
        choices = openai_json.get("choices", [])
        content_blocks: List[Dict[str, Any]] = []
        stop_reason = "end_turn"

        if choices:
            msg = choices[0].get("message", {})
            if msg.get("content"):
                content_blocks.append({"type": "text", "text": msg["content"]})
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except Exception:
                    args = {}
                content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:10]}"),
                        "name": fn.get("name", ""),
                        "input": args,
                    }
                )
                stop_reason = "tool_use"

            finish_reason = choices[0].get("finish_reason")
            if finish_reason == "length":
                stop_reason = "max_tokens"

        usage = openai_json.get("usage", {})
        in_tokens = usage.get("prompt_tokens", max(1, len(user_prompt) // 4))
        out_tokens = usage.get(
            "completion_tokens", max(1, sum(len(str(b)) for b in content_blocks) // 4)
        )

        return {
            "id": req_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content_blocks,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": in_tokens,
                "output_tokens": out_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        }
