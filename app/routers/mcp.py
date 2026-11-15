"""Model Context Protocol (MCP) Security Proxy & JSON-RPC Firewall.

Inspects incoming MCP tool executions (tools/call) for destructive shell commands,
unsafe SQL, and SSRF before forwarding to local or remote MCP servers.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import urllib.parse
import uuid
from typing import Any, Dict, Optional, Set, Tuple

import httpx
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.services.agent_guard import AgentToolGuard

logger = logging.getLogger("sentinelgate.mcp")

router = APIRouter(prefix="/v1/mcp", tags=["Model Context Protocol (MCP)"])

# Allowed default target schemes
ALLOWED_MCP_SCHEMES: Set[str] = {"http", "https"}

# Disallowed loopback/metadata hostnames and internal suffixes
PROHIBITED_HOSTNAMES: Set[str] = {
    "localhost",
    "ip6-localhost",
    "ip6-loopback",
    "metadata.google.internal",
}
PROHIBITED_SUFFIXES: Tuple[str, ...] = (
    ".local",
    ".internal",
    ".lan",
    ".corp",
    ".home",
)


def validate_mcp_target(
    target: Optional[str],
    allowed_hosts: Optional[Set[str]] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """Validate and sanitize an upstream MCP target URL to prevent SSRF vulnerabilities.

    Enforces:
    1. Valid HTTP/HTTPS scheme only.
    2. Non-empty hostname with length <= 2048 chars.
    3. No embedded credentials (user:pass@host).
    4. Rejection of cloud metadata endpoints (169.254.169.254, metadata.google.internal).
    5. Rejection of private RFC 1918, loopback, link-local, multicast, and reserved IP ranges.
    6. Rejection of internal domain suffixes (.local, .internal, etc.).
    7. Optional strict host allowlist validation when configured.

    Returns:
        (is_valid, sanitized_url, error_message)
    """
    if not target or not isinstance(target, str):
        return False, None, "MCP target must be a non-empty string."

    target_clean = target.strip()
    if len(target_clean) > 2048:
        return False, None, "MCP target URL exceeds maximum permitted length of 2048 characters."

    try:
        parsed = urllib.parse.urlsplit(target_clean)
    except Exception:
        return False, None, "Failed to parse MCP target URL."

    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_MCP_SCHEMES:
        return (
            False,
            None,
            f"Disallowed scheme '{parsed.scheme}'. Only HTTP and HTTPS protocols are permitted.",
        )

    hostname = parsed.hostname
    if not hostname:
        return False, None, "MCP target URL must include a valid hostname."

    # Disallow embedded credentials
    if parsed.username or parsed.password:
        return False, None, "Embedded user credentials in MCP target URL are strictly prohibited."

    # Intercept known SSRF / cloud metadata regex patterns
    is_safe, threat_type, detail = AgentToolGuard.inspect_text_payload(target_clean)
    if not is_safe:
        return False, None, f"Target URL violates SSRF security policy: {detail}"

    # Evaluate IP address safety (including IPv6 brackets and decimal/hex/octal encodings)
    clean_host = hostname.strip("[]")
    ip = None
    try:
        ip = ipaddress.ip_address(clean_host)
    except ValueError:
        try:
            raw_bytes = socket.inet_aton(clean_host)
            ip = ipaddress.IPv4Address(raw_bytes)
        except (OSError, ValueError):
            pass

    if ip is not None:
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return (
                False,
                None,
                f"Target IP address '{hostname}' is in a prohibited restricted range (loopback, private, or link-local).",
            )
    else:
        # Hostname is a domain name rather than an IP literal
        lowered_host = hostname.lower()
        if lowered_host in PROHIBITED_HOSTNAMES:
            return False, None, f"Prohibited loopback/metadata host '{hostname}'."
        if any(lowered_host.endswith(suffix) for suffix in PROHIBITED_SUFFIXES):
            return False, None, f"Prohibited internal network domain '{hostname}'."

    # Check configured host allowlist (if provided)
    if allowed_hosts:
        normalized_allowed = {h.lower().strip() for h in allowed_hosts if h.strip()}
        if normalized_allowed and hostname.lower() not in normalized_allowed:
            return (
                False,
                None,
                f"Host '{hostname}' is not in the configured allowed MCP upstream hosts list.",
            )

    # Reconstruct sanitized URL safely
    port_str = f":{parsed.port}" if parsed.port else ""
    netloc = f"{hostname.lower()}{port_str}"
    path = parsed.path if parsed.path else "/"
    query = f"?{parsed.query}" if parsed.query else ""
    sanitized_url = f"{scheme}://{netloc}{path}{query}"

    return True, sanitized_url, None


class MCPJsonRpcRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: Optional[Any] = 1
    method: str
    params: Optional[Dict[str, Any]] = None


@router.post("/proxy")
@router.post("/inspect")
async def mcp_proxy(
    req: Dict[str, Any],
    request: Request,
    x_mcp_target: Optional[str] = Header(None, alias="X-MCP-Target"),
):
    """Inspect and proxy MCP JSON-RPC requests through the SentinelGate Agent Firewall."""
    evals = request.app.state.evals
    req_id = f"mcp-{uuid.uuid4().hex[:12]}"

    is_safe, threat_type, detail = AgentToolGuard.inspect_mcp_message(req)
    if not is_safe:
        logger.warning(f"[AGENT FIREWALL] Intercepted dangerous MCP tool execution: {detail}")
        evals.log_guardrail_event(
            request_id=req_id,
            event_type="MCP_TOOL_BLOCKED",
            threat_detail=detail or "Blocked by Agent Tool Guard",
            blocked=True,
        )
        return JSONResponse(
            content={
                "jsonrpc": req.get("jsonrpc", "2.0"),
                "id": req.get("id", 1),
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": f"SentinelGate Agent Security Firewall blocked tool execution: {detail}. Operation halted by policy.",
                        }
                    ],
                    "isError": True,
                },
            },
            headers={
                "X-Agent-Firewall-Blocked": "true",
                "X-Agent-Firewall-Reason": str(threat_type),
            },
        )

    # If an upstream MCP target server was provided, validate and forward
    if x_mcp_target:
        env_allowed = os.getenv("SENTINELGATE_ALLOWED_MCP_HOSTS", "")
        allowed_hosts_set = (
            {h.strip() for h in env_allowed.split(",") if h.strip()} if env_allowed else None
        )

        is_valid, safe_target, err_msg = validate_mcp_target(
            x_mcp_target, allowed_hosts=allowed_hosts_set
        )
        if not is_valid or not safe_target:
            logger.warning(f"[AGENT FIREWALL] SSRF attempt blocked on X-MCP-Target: {err_msg}")
            evals.log_guardrail_event(
                request_id=req_id,
                event_type="MCP_SSRF_BLOCKED",
                threat_detail=err_msg or "SSRF attempt blocked by Agent Tool Guard",
                blocked=True,
            )
            return JSONResponse(
                status_code=400,
                content={
                    "jsonrpc": req.get("jsonrpc", "2.0"),
                    "id": req.get("id", 1),
                    "error": {
                        "code": -32602,
                        "message": f"SentinelGate Agent Security Firewall rejected upstream target: {err_msg}",
                    },
                },
                headers={
                    "X-Agent-Firewall-Blocked": "true",
                    "X-Agent-Firewall-Reason": "SSRF_DISALLOWED_TARGET",
                },
            )

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                upstream_res = await client.post(safe_target, json=req)
                return JSONResponse(
                    content=upstream_res.json(),
                    status_code=upstream_res.status_code,
                    headers={"X-Agent-Firewall-Blocked": "false"},
                )
        except Exception as e:
            logger.warning(f"Upstream MCP forward failed: {e}")
            return JSONResponse(
                content={
                    "jsonrpc": req.get("jsonrpc", "2.0"),
                    "id": req.get("id", 1),
                    "error": {
                        "code": -32000,
                        "message": "Upstream MCP forward failed: communication or network error.",
                    },
                },
                status_code=502,
            )

    # Standalone validation response
    return JSONResponse(
        content={
            "jsonrpc": req.get("jsonrpc", "2.0"),
            "id": req.get("id", 1),
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": "SentinelGate Agent Security Firewall verified MCP payload: Safe.",
                    }
                ],
                "isError": False,
            },
        },
        headers={"X-Agent-Firewall-Blocked": "false"},
    )
