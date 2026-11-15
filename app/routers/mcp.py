"""Model Context Protocol (MCP) Security Proxy & JSON-RPC Firewall.

Inspects incoming MCP tool executions (tools/call) for destructive shell commands,
unsafe SQL, and SSRF before forwarding to local or remote MCP servers.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.services.agent_guard import AgentToolGuard

logger = logging.getLogger("sentinelgate.mcp")

router = APIRouter(prefix="/v1/mcp", tags=["Model Context Protocol (MCP)"])


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

    # If an upstream MCP target server was provided, forward the request
    if x_mcp_target:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                upstream_res = await client.post(x_mcp_target, json=req)
                return JSONResponse(
                    content=upstream_res.json(),
                    status_code=upstream_res.status_code,
                    headers={"X-Agent-Firewall-Blocked": "false"},
                )
        except Exception as e:
            return JSONResponse(
                content={
                    "jsonrpc": req.get("jsonrpc", "2.0"),
                    "id": req.get("id", 1),
                    "error": {"code": -32000, "message": f"Upstream MCP forward failed: {e}"},
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
