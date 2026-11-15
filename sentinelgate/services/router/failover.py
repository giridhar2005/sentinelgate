from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import httpx

from sentinelgate.services.router.providers.anthropic import (
    build_anthropic_payload,
    convert_anthropic_to_openai,
)
from sentinelgate.services.router.providers.gemini import (
    build_gemini_payload,
    convert_gemini_to_openai,
)
from sentinelgate.services.router.providers.sandbox import (
    generate_sandbox_response,
    stream_sandbox_response,
)

logger = logging.getLogger("sentinelgate.router.failover")


async def stream_response_lines(
    client: httpx.AsyncClient,
    p_type: str,
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    model: str,
) -> AsyncGenerator[str, None]:
    req_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    if p_type in ("openai", "openai_compatible", "ollama"):
        payload["stream"] = True

    async with client.stream("POST", url, headers=headers, json=payload, timeout=60.0) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line:
                continue
            if p_type == "gemini":
                if line.startswith("data: "):
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        yield "data: [DONE]\n\n"
                        continue
                    try:
                        g_data = json.loads(data_str)
                        parts = (
                            g_data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])
                        )
                        text = parts[0].get("text", "") if parts else ""
                        chunk = {
                            "id": req_id,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [
                                {"index": 0, "delta": {"content": text}, "finish_reason": None}
                            ],
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"
                    except Exception:
                        continue
            elif p_type == "anthropic":
                if line.startswith("data: "):
                    data_str = line[6:].strip()
                    try:
                        a_data = json.loads(data_str)
                        evt_type = a_data.get("type")
                        if evt_type == "content_block_start":
                            block = a_data.get("content_block", {})
                            if block.get("type") == "tool_use":
                                chunk = {
                                    "id": req_id,
                                    "object": "chat.completion.chunk",
                                    "created": int(time.time()),
                                    "model": model,
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {
                                                "role": "assistant",
                                                "tool_calls": [
                                                    {
                                                        "index": a_data.get("index", 0),
                                                        "id": block.get("id"),
                                                        "type": "function",
                                                        "function": {
                                                            "name": block.get("name"),
                                                            "arguments": "",
                                                        },
                                                    }
                                                ],
                                            },
                                            "finish_reason": None,
                                        }
                                    ],
                                }
                                yield f"data: {json.dumps(chunk)}\n\n"
                        elif evt_type == "content_block_delta":
                            delta_data = a_data.get("delta", {})
                            d_type = delta_data.get("type")
                            if d_type == "text_delta":
                                delta_text = delta_data.get("text", "")
                                chunk = {
                                    "id": req_id,
                                    "object": "chat.completion.chunk",
                                    "created": int(time.time()),
                                    "model": model,
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {"content": delta_text},
                                            "finish_reason": None,
                                        }
                                    ],
                                }
                                yield f"data: {json.dumps(chunk)}\n\n"
                            elif d_type == "input_json_delta":
                                partial_json = delta_data.get("partial_json", "")
                                chunk = {
                                    "id": req_id,
                                    "object": "chat.completion.chunk",
                                    "created": int(time.time()),
                                    "model": model,
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {
                                                "tool_calls": [
                                                    {
                                                        "index": a_data.get("index", 0),
                                                        "function": {"arguments": partial_json},
                                                    }
                                                ]
                                            },
                                            "finish_reason": None,
                                        }
                                    ],
                                }
                                yield f"data: {json.dumps(chunk)}\n\n"
                        elif evt_type == "message_stop":
                            yield "data: [DONE]\n\n"
                    except Exception:
                        continue
            else:
                yield f"{line}\n\n"


async def execute_failover_loop(
    request_data: Dict[str, Any],
    stream: bool,
    candidates: List[Tuple[str, str]],
    providers: Dict[str, Any],
    client: httpx.AsyncClient,
    is_sandbox: bool,
    chaos_config: Dict[str, Any],
    get_key_fn: Callable[[str], str],
    record_success_fn: Callable[[str], None],
    record_failure_fn: Callable[[str, int], None],
) -> Tuple[Union[Dict[str, Any], AsyncGenerator[str, None]], str, str]:
    last_error: Optional[str] = None
    chaos_enabled = chaos_config.get("enabled", False)
    chaos_target = chaos_config.get("target_provider", "openai")
    chaos_code = chaos_config.get("status_code", 429)

    for p, m in candidates:
        if chaos_enabled and p == chaos_target:
            logger.warning(
                f"[CHAOS FAULT INJECTION] Injected HTTP {chaos_code} failure on provider '{p}'"
            )
            record_failure_fn(p, chaos_code)
            last_error = f"Chaos fault injection: simulated HTTP {chaos_code} on {p}"
            continue

        if is_sandbox:
            # Zero-key sandbox simulation
            import asyncio

            await asyncio.sleep(0.015)
            if stream:
                gen = stream_sandbox_response(m, request_data.get("messages", []))
                record_success_fn(p)
                return gen, p, m
            else:
                res_json = generate_sandbox_response(m, p, request_data.get("messages", []))
                record_success_fn(p)
                return res_json, p, m

        p_cfg = providers.get(p)
        p_type = (
            getattr(p_cfg, "type", "openai")
            if hasattr(p_cfg, "type")
            else (p_cfg.get("type", "openai") if isinstance(p_cfg, dict) else "openai")
        )
        base_url = (
            getattr(p_cfg, "base_url", "")
            if hasattr(p_cfg, "base_url")
            else (p_cfg.get("base_url", "") if isinstance(p_cfg, dict) else "")
        ).rstrip("/")
        keys = (
            getattr(p_cfg, "api_keys", [])
            if hasattr(p_cfg, "api_keys")
            else (p_cfg.get("api_keys", []) if isinstance(p_cfg, dict) else [])
        )
        num_keys = max(1, len(keys))

        for _ in range(num_keys):
            api_key = get_key_fn(p)
            headers = {"Content-Type": "application/json"}
            if "_headers" in request_data and isinstance(request_data["_headers"], dict):
                headers.update(request_data["_headers"])
            url = ""
            payload = dict(request_data)
            payload.pop("_headers", None)
            payload["model"] = m

            if p_type in ("openai", "openai_compatible", "ollama"):
                url = f"{base_url}/chat/completions"
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
            elif p_type == "anthropic":
                url = f"{base_url}/messages"
                headers["anthropic-version"] = "2023-06-01"
                if api_key:
                    headers["x-api-key"] = api_key
                payload = build_anthropic_payload(request_data, m, stream=stream)
            elif p_type == "gemini":
                action = "streamGenerateContent?alt=sse&" if stream else "generateContent?"
                url = f"{base_url}/models/{m}:{action}key={api_key}"
                payload = build_gemini_payload(request_data)

            try:
                if stream:
                    gen = stream_response_lines(client, p_type, url, headers, payload, m)
                    record_success_fn(p)
                    return gen, p, m
                else:
                    resp = await client.post(url, headers=headers, json=payload, timeout=60.0)
                    if resp.status_code in (429, 500, 502, 503):
                        record_failure_fn(p, resp.status_code)
                        logger.warning(
                            f"Provider {p} returned {resp.status_code}. Failing over/rotating."
                        )
                        last_error = f"HTTP {resp.status_code}: {resp.text}"
                        continue

                    resp.raise_for_status()
                    record_success_fn(p)
                    res_json = resp.json()
                    if p_type == "gemini":
                        res_json = convert_gemini_to_openai(res_json, m)
                    elif p_type == "anthropic":
                        res_json = convert_anthropic_to_openai(res_json, m)
                    return res_json, p, m

            except httpx.HTTPStatusError as e:
                if e.response.status_code in (429, 500, 502, 503):
                    record_failure_fn(p, e.response.status_code)
                    last_error = str(e)
                    continue
                raise e
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                record_failure_fn(p, 503)
                logger.warning(f"Connection error contacting {p}: {e}")
                last_error = str(e)
                break

    raise RuntimeError(f"All providers and fallbacks exhausted. Last error: {last_error}")
