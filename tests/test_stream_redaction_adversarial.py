from __future__ import annotations

import json

import pytest

from sentinelgate.services.streaming_redactor import (
    SlidingWindowStreamRedactor,
    sse_sliding_window_redactor,
)


def test_sliding_window_stream_redactor_unit_single_char_splits():
    """Adversarial test: secret is fed in 1-character increments."""
    redactor = SlidingWindowStreamRedactor(lookahead_chars=32)
    secret = "sk-proj-999988887777666655554444333322221111"

    emitted_parts = []
    # Feed 1 character at a time
    for ch in f"Key is {secret} for testing.":
        chunk = redactor.process_delta(ch, is_final=False)
        if chunk:
            emitted_parts.append(chunk)

    # Flush final
    emitted_parts.append(redactor.process_delta("", is_final=True))
    full_output = "".join(emitted_parts)

    assert secret not in full_output
    assert "__SG_SECRET_" in full_output
    assert "Key is " in full_output
    assert " for testing." in full_output


def test_sliding_window_stream_redactor_multi_secret_chunking():
    """Adversarial test: multiple distinct secrets split across 2-to-3 character slices."""
    redactor = SlidingWindowStreamRedactor(lookahead_chars=40)
    raw_text = (
        "AWS: AKIAIOSFODNN7EXAMPLE and DB: postgresql://admin:secret_pass_123@db.prod:5432/orders"
    )

    # Slice raw_text into random 2-to-3 character slices
    slices = []
    i = 0
    while i < len(raw_text):
        step = 2 if (i % 2 == 0) else 3
        slices.append(raw_text[i : i + step])
        i += step

    emitted = []
    for s in slices:
        chunk = redactor.process_delta(s, is_final=False)
        if chunk:
            emitted.append(chunk)

    emitted.append(redactor.process_delta("", is_final=True))
    full_output = "".join(emitted)

    assert "AKIAIOSFODNN7EXAMPLE" not in full_output
    assert "secret_pass_123" not in full_output
    assert "__SG_SECRET_" in full_output


@pytest.mark.asyncio
async def test_sse_sliding_window_redactor_async_generator():
    """Test full async generator over SSE protocol frames."""
    secret = "sk-proj-aabbccddeeff001122334455"
    full_text = f"The secret API token is {secret}."

    # Simulate SSE chunks where secret is sliced in half
    part1 = full_text[:28]  # "The secret API token is sk-pr"
    part2 = full_text[28:]  # "oj-aabbccddeeff001122334455."

    async def mock_upstream():
        chunk1 = {"choices": [{"delta": {"content": part1}}]}
        yield f"data: {json.dumps(chunk1)}\n\n"
        chunk2 = {"choices": [{"delta": {"content": part2}}]}
        yield f"data: {json.dumps(chunk2)}\n\n"
        yield "data: [DONE]\n\n"

    collected_content = []
    has_done = False

    async for sse_line in sse_sliding_window_redactor(mock_upstream(), lookahead_chars=32):
        if sse_line.strip() == "data: [DONE]":
            has_done = True
        elif sse_line.startswith("data: "):
            try:
                data = json.loads(sse_line[6:].strip())
                content = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                if content:
                    collected_content.append(content)
            except Exception:
                pass

    result_text = "".join(collected_content)
    assert has_done is True
    assert secret not in result_text
    assert "__SG_SECRET_" in result_text
    assert "The secret API token is" in result_text
