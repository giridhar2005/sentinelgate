from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from fastapi.responses import JSONResponse

from sentinelgate.services.guardrails import (
    ContextCompactor,
    PIIGuardrail,
    PromptOptimizer,
    SecretRedactor,
    SecurityInspector,
)


def make_error_response(
    protocol: str,
    message: str,
    error_type: str = "invalid_request_error",
    code: Optional[str] = None,
    status_code: int = 400,
) -> JSONResponse:
    if protocol == "anthropic":
        return JSONResponse(
            status_code=status_code,
            content={
                "type": "error",
                "error": {
                    "type": error_type,
                    "message": message,
                },
            },
        )
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "code": code,
            }
        },
    )


def run_inbound_guardrails(
    messages: List[Dict[str, Any]],
    secret_action: str,
    req_id: str,
    evals: Any,
    protocol: str,
    t_perf_start: float,
) -> Tuple[
    Optional[JSONResponse],
    List[Dict[str, Any]],
    Dict[str, str],
    Dict[str, str],
    int,
]:
    """Execute pre-routing inbound security: secret leak check/redact, prompt injection, and PII masking."""
    secret_mapping: Dict[str, str] = {}
    scrubbed_count = 0

    if secret_action == "block":
        is_secret_safe, secret_threat = SecurityInspector.inspect_secrets(messages)
        if not is_secret_safe:
            if evals:
                evals.log_guardrail_event(req_id, "SECRET_LEAK", secret_threat or "", blocked=True)
            return (
                make_error_response(
                    protocol=protocol,
                    message=f"Security Violation: {secret_threat}",
                    error_type="security_guardrail_violation",
                    code="secret_leak_detected",
                    status_code=400,
                ),
                messages,
                {},
                {},
                0,
            )
    elif secret_action == "redact":
        messages, secret_mapping, scrubbed_count = SecretRedactor.redact_messages(messages)
        if scrubbed_count > 0 and evals:
            evals.log_guardrail_event(
                req_id,
                "SECRET_REDACTED",
                f"Scrubbed {scrubbed_count} secret credential(s)",
                blocked=False,
            )

    is_inj_safe, inj_threat = SecurityInspector.inspect_injection(messages)
    if not is_inj_safe:
        if evals:
            evals.log_guardrail_event(req_id, "PROMPT_INJECTION", inj_threat or "", blocked=True)
        return (
            make_error_response(
                protocol=protocol,
                message=f"Prompt Security Violation: {inj_threat}",
                error_type="invalid_request_error",
                code="prompt_injection_detected",
                status_code=400,
            ),
            messages,
            {},
            {},
            0,
        )

    messages = ContextCompactor.compact(messages)
    messages = PromptOptimizer.optimize_messages(messages)
    masked_messages, pii_mapping = PIIGuardrail.mask_messages(messages)

    return None, masked_messages, secret_mapping, pii_mapping, scrubbed_count
