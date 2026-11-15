from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any, Dict, List


def generate_sandbox_response(
    model: str,
    provider: str,
    messages: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Generate a deterministic, realistic LLM completion for zero-key sandbox evaluation."""
    user_content = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_content = str(m.get("content", ""))
            break
    if not user_content:
        user_content = "Default prompt"

    lower = user_content.lower()
    prompt_tokens = max(1, sum(len(str(m.get("content", ""))) // 4 for m in messages))

    if (
        "rm -rf" in lower
        or "agent tool attack" in lower
        or "drop table" in lower
        or "bash(" in lower
    ):
        return {
            "id": f"chatcmpl-sandbox-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"call_sandbox_{uuid.uuid4().hex[:8]}",
                                "type": "function",
                                "function": {
                                    "name": "bash",
                                    "arguments": json.dumps(
                                        {"command": "rm -rf / --no-preserve-root"}
                                    ),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": 15,
                "total_tokens": prompt_tokens + 15,
            },
        }

    if "raft" in lower:
        text = (
            "Raft is a leader-based consensus algorithm designed for distributed state machine replication. "
            "It manages log replication, leader elections, and joint-consensus cluster membership changes "
            "with provable formal safety invariants and predictable state transitions."
        )
    elif "quick sort" in lower or "quicksort" in lower:
        text = (
            "QuickSort is an in-place, divide-and-conquer sorting algorithm with average-case time complexity "
            "of O(N log N). It selects a pivot, partitions elements into sub-arrays according to the pivot, "
            "and recursively sorts each sub-array with O(log N) stack overhead."
        )
    elif "binary search" in lower:
        text = (
            "Binary search operates on sorted arrays by repeatedly halving the search interval. "
            "Comparing the target with the median index element achieves logarithmic O(log N) runtime "
            "with O(1) auxiliary space complexity."
        )
    elif "hello" in lower or "hi" in lower:
        text = (
            "Hello! SentinelGate is active in deterministic zero-key sandbox mode with low-latency local "
            "inference simulation, semantic caching, and full OpenAI REST compatibility."
        )
    else:
        snippet = user_content[:70].strip()
        text = (
            f"SentinelGate Sandbox [{provider}/{model}]: Verified response for prompt '{snippet}'. "
            "Executed with deterministic token telemetry and zero cloud egress latency."
        )

    completion_tokens = max(1, len(text) // 4)

    return {
        "id": f"chatcmpl-sandbox-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


async def stream_sandbox_response(
    model: str,
    messages: List[Dict[str, Any]],
) -> AsyncGenerator[str, None]:
    """Stream a deterministic response token by token via SSE."""
    res = generate_sandbox_response(model, "sandbox", messages)
    text = res["choices"][0]["message"]["content"]
    req_id = f"chatcmpl-sandbox-{uuid.uuid4().hex[:12]}"
    words = text.split(" ")
    for i, word in enumerate(words):
        token = word + (" " if i < len(words) - 1 else "")
        chunk = {
            "id": req_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {"content": token}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(chunk)}\n\n"
        await asyncio.sleep(0.015)

    done_chunk = {
        "id": req_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(done_chunk)}\n\n"
    yield "data: [DONE]\n\n"
