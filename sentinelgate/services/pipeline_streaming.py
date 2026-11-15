from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any, Dict, List

from fastapi.responses import StreamingResponse

from sentinelgate.core.telemetry_otlp import TraceContext
from sentinelgate.services.agent_guard import AgentToolGuard
from sentinelgate.services.streaming_redactor import SlidingWindowStreamRedactor

logger = logging.getLogger("sentinelgate.pipeline_streaming")


def convert_openai_to_anthropic(
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


async def handle_streaming_pipeline(
    raw_gen: AsyncGenerator[str, None],
    protocol: str,
    req_id: str,
    chosen_p: str,
    chosen_m: str,
    user_prompt: str,
    rate_limit_headers: Dict[str, str],
    trace_ctx: TraceContext,
    secret_mapping: Dict[str, str],
    router_instance: Any,
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
        auto_meta = getattr(router_instance, "last_routing_metadata", {})
        headers["X-Auto-Route"] = f"{chosen_p}/{chosen_m}"
        if auto_meta:
            headers["X-Auto-Complexity"] = (
                f"{auto_meta.get('classification', 'simple')} ({auto_meta.get('score', 0)}/100)"
            )
    headers.update(rate_limit_headers)

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
                break
    except Exception as e:
        logger.warning(f"Error during stream peeking: {e}")

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
            refusal_msg = (
                f"SentinelGate Agent Security Firewall blocked tool execution: {tool_threat_msg}."
            )
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

        return StreamingResponse(anthropic_outer(), media_type="text/event-stream", headers=headers)

    return StreamingResponse(stream_generator(), media_type="text/event-stream", headers=headers)
