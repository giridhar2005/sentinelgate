from __future__ import annotations

import time
import uuid
from typing import Any, Dict


def build_gemini_payload(req: Dict[str, Any]) -> Dict[str, Any]:
    contents = []
    system_instruction = None
    for msg in req.get("messages", []):
        role = msg.get("role")
        text = msg.get("content", "")
        if role == "system":
            system_instruction = {"parts": [{"text": text}]}
        else:
            gemini_role = "model" if role == "assistant" else "user"
            contents.append({"role": gemini_role, "parts": [{"text": text}]})

    body: Dict[str, Any] = {"contents": contents}
    if system_instruction:
        body["system_instruction"] = system_instruction

    gen_config: Dict[str, Any] = {}
    if "temperature" in req and req["temperature"] is not None:
        gen_config["temperature"] = req["temperature"]
    if "max_tokens" in req and req["max_tokens"] is not None:
        gen_config["maxOutputTokens"] = req["max_tokens"]
    if req.get("response_format", {}).get("type") == "json_object":
        gen_config["responseMimeType"] = "application/json"
    if gen_config:
        body["generationConfig"] = gen_config
    return body


def convert_gemini_to_openai(gemini_data: Dict[str, Any], model: str) -> Dict[str, Any]:
    candidates = gemini_data.get("candidates", [])
    text = ""
    finish_reason = "stop"
    if candidates:
        parts = candidates[0].get("content", {}).get("parts", [])
        if parts:
            text = parts[0].get("text", "")
        finish_reason = candidates[0].get("finishReason", "stop").lower()

    usage = gemini_data.get("usageMetadata", {})
    prompt_tokens = usage.get("promptTokenCount", max(1, len(str(gemini_data)) // 4))
    completion_tokens = usage.get("candidatesTokenCount", max(1, len(text) // 4))

    return {
        "id": f"chatcmpl-gemini-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
