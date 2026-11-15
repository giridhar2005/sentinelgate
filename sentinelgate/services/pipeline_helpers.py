from __future__ import annotations

from typing import Any, Dict, List, Optional

from sentinelgate.core.telemetry_otlp import TraceContext


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


def build_upstream_headers(
    req_id: str,
    chosen_p: str,
    lat_ms: float,
    tool_blocked: bool,
    tool_detail: Optional[str],
    trace_ctx: TraceContext,
    scrubbed_count: int,
    hedge_meta: Optional[Dict[str, Any]],
    model: str,
    chosen_m: str,
    router_instance: Any,
    rate_limit_headers: Dict[str, str],
    cost_usd: float,
    auto_saved_usd: float,
) -> Dict[str, str]:
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

    if model == "auto":
        headers["X-Auto-Route"] = f"{chosen_p}/{chosen_m}"
        headers["X-Auto-Saved-USD"] = f"{auto_saved_usd:.6f}"
        auto_meta = getattr(router_instance, "last_routing_metadata", {})
        if auto_meta:
            headers["X-Auto-Complexity"] = (
                f"{auto_meta.get('classification', 'simple')} ({auto_meta.get('score', 0)}/100)"
            )

    headers.update(rate_limit_headers)
    return headers


def build_cache_hit_response(
    cached_resp: Dict[str, Any],
    t_perf_start: float,
    pii_mapping: Dict[str, str],
    secret_mapping: Dict[str, str],
    req_id: str,
    model: str,
    trace_ctx: TraceContext,
    scrubbed_count: int,
    rate_limit_headers: Dict[str, str],
    evals: Any,
    protocol: str,
    user_prompt: str,
    to_anthropic_fn: Any,
) -> Any:
    import time

    from fastapi.responses import JSONResponse

    from sentinelgate.services.guardrails import PIIGuardrail, SecretRedactor

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

    if evals:
        usage = resp_payload.get("usage", {})
        in_tok = usage.get("prompt_tokens", len(user_prompt) // 4)
        out_tok = usage.get("completion_tokens", 10)
        evals.log_request(
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
        anthropic_json = to_anthropic_fn(resp_payload, req_id, model, user_prompt)
        return JSONResponse(status_code=200, content=anthropic_json, headers=hdrs)

    return JSONResponse(status_code=200, content=resp_payload, headers=hdrs)
