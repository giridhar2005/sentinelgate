from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict


def build_anthropic_payload(
    req: Dict[str, Any], model: str, stream: bool = False
) -> Dict[str, Any]:
    system_text = ""
    chat_messages = []
    for m in req.get("messages", []):
        role = m.get("role")
        content = m.get("content", "")
        if role == "system":
            system_text += f"\n{content}".strip()
        else:
            anthropic_role = "assistant" if role == "assistant" else "user"
            chat_messages.append({"role": anthropic_role, "content": content})

    if not chat_messages:
        chat_messages.append({"role": "user", "content": "Hello"})

    body: Dict[str, Any] = {
        "model": model,
        "messages": chat_messages,
        "max_tokens": req.get("max_tokens") or 2048,
    }
    if system_text:
        body["system"] = system_text
    if "temperature" in req and req["temperature"] is not None:
        body["temperature"] = req["temperature"]
    if stream:
        body["stream"] = True
    return body


def convert_anthropic_to_openai(anth_data: Dict[str, Any], model: str) -> Dict[str, Any]:
    content_blocks = anth_data.get("content", [])
    text_parts = []
    tool_calls = []
    for b in content_blocks:
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

    text = "".join(text_parts)
    usage = anth_data.get("usage", {})
    prompt_tokens = usage.get("input_tokens", max(1, len(str(anth_data)) // 4))
    completion_tokens = usage.get("output_tokens", max(1, len(text) // 4))

    msg: Dict[str, Any] = {"role": "assistant", "content": text if text else None}
    if tool_calls:
        msg["tool_calls"] = tool_calls

    stop_r = anth_data.get("stop_reason", "stop")
    if stop_r == "tool_use":
        finish_reason = "tool_calls"
    elif stop_r == "max_tokens":
        finish_reason = "length"
    else:
        finish_reason = "stop"

    return {
        "id": f"chatcmpl-ant-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": msg,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
