from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app.core.security import make_openai_error
from app.core.telemetry_otlp import OTLPExporter
from app.services.gateway_pipeline import GatewayPipeline

logger = logging.getLogger("sentinelgate.v1")

otlp_exporter = OTLPExporter()

router = APIRouter(prefix="/v1", tags=["OpenAI & Anthropic V1"])


class ChatMessage(BaseModel):
    role: str
    content: Any


class ChatCompletionRequest(BaseModel):
    model: str = "gpt-4o-mini"
    messages: List[Dict[str, Any]]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Any] = None
    hedge: Optional[bool] = False

    model_config = {"extra": "allow"}


class EmbeddingsRequest(BaseModel):
    model: str
    input: Any


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
    temperature: Optional[float] = 0.7


def _get_pipeline(request: Request) -> GatewayPipeline:
    pipeline = getattr(request.app.state, "pipeline", None)
    if pipeline is not None:
        return pipeline
    return GatewayPipeline(
        router=request.app.state.router,
        cache=request.app.state.cache,
        budget=request.app.state.budget,
        evals=getattr(request.app.state, "evals", None),
        virtual_keys=getattr(request.app.state, "virtual_keys", None),
        config=getattr(request.app.state, "config", None),
    )


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
    pipeline = _get_pipeline(request)
    tools_dict = (
        [t.model_dump() if hasattr(t, "model_dump") else t for t in req.tools]
        if req.tools
        else None
    )
    messages_dict = [m.model_dump() if hasattr(m, "model_dump") else m for m in req.messages]

    return await pipeline.execute_chat(
        request=request,
        messages=messages_dict,
        model=req.model,
        protocol="openai",
        tools=tools_dict,
        temperature=req.temperature or 0.7,
        max_tokens=req.max_tokens or 2048,
        stream=bool(req.stream),
        bypass_cache=(x_no_cache is not None and x_no_cache.lower() in ("true", "1")),
        hedging_requested=(
            (x_hedge is not None and x_hedge.lower() in ("true", "1")) or bool(req.hedge)
        ),
        extra_payload=req.model_dump(exclude_unset=True),
    )


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
    auth_ctx = getattr(request.state, "auth_context", None)
    tenant_id = auth_ctx.token if auth_ctx else "default"
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
            tenant_id=tenant_id,
            temperature=item.temperature if item.temperature is not None else 0.7,
        )
    return JSONResponse(content={"status": "success", "prewarmed_count": len(items)})


@router.get("/evals/export")
async def export_evals(request: Request, limit: Optional[int] = 100):
    app_evals = request.app.state.evals
    return JSONResponse(content={"dataset": app_evals.export_golden_dataset(limit=limit or 100)})


@router.post("/messages")
async def anthropic_messages(
    request: Request,
    background_tasks: BackgroundTasks,
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    authorization: Optional[str] = Header(None),
    anthropic_version: Optional[str] = Header("2023-06-01"),
):
    """Anthropic Messages API Adapter (/v1/messages).

    Translates Anthropic Messages requests to SentinelGate's internal router,
    enabling drop-in compatibility with Claude Code CLI and the Anthropic SDK.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "Malformed JSON request body.",
                },
            },
        )

    requested_model = body.get("model", "auto")
    raw_messages = body.get("messages", [])
    system_prompt = body.get("system", "")
    max_tokens = body.get("max_tokens", 2048)
    temperature = body.get("temperature", 0.7)
    stream = body.get("stream", False)
    anthropic_tools = body.get("tools", [])

    openai_messages: List[Dict[str, Any]] = []
    if system_prompt:
        if isinstance(system_prompt, list):
            sys_text = " ".join(b.get("text", "") for b in system_prompt if isinstance(b, dict))
        else:
            sys_text = str(system_prompt)
        if sys_text.strip():
            openai_messages.append({"role": "system", "content": sys_text.strip()})

    for m in raw_messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            text_parts = []
            tool_calls = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                b_type = b.get("type")
                if b_type == "text":
                    text_parts.append(b.get("text", ""))
                elif b_type == "tool_use":
                    tool_calls.append(
                        {
                            "id": b.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                            "type": "function",
                            "function": {
                                "name": b.get("name", ""),
                                "arguments": json.dumps(b.get("input", {})),
                            },
                        }
                    )
                elif b_type == "tool_result":
                    openai_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": b.get("tool_use_id", ""),
                            "content": (
                                b.get("content", "")
                                if isinstance(b.get("content"), str)
                                else json.dumps(b.get("content"))
                            ),
                        }
                    )
            if role == "assistant":
                asst_msg: Dict[str, Any] = {
                    "role": "assistant",
                    "content": " ".join(text_parts) if text_parts else None,
                }
                if tool_calls:
                    asst_msg["tool_calls"] = tool_calls
                openai_messages.append(asst_msg)
            elif role == "user" and text_parts:
                openai_messages.append({"role": "user", "content": " ".join(text_parts)})
        else:
            openai_messages.append({"role": role, "content": str(content)})

    if not openai_messages:
        return JSONResponse(
            status_code=400,
            content={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "Request must contain at least one message.",
                },
            },
        )

    openai_tools = None
    if anthropic_tools:
        openai_tools = []
        for t in anthropic_tools:
            openai_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {}),
                    },
                }
            )

    pipeline = _get_pipeline(request)
    resp = await pipeline.execute_chat(
        request=request,
        messages=openai_messages,
        model=requested_model,
        protocol="anthropic",
        system_prompt=system_prompt if isinstance(system_prompt, str) else "",
        tools=openai_tools,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=bool(stream),
    )
    if isinstance(resp, (JSONResponse, StreamingResponse)):
        resp.headers["anthropic-version"] = anthropic_version or "2023-06-01"
    return resp
