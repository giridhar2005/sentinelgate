"""Property-based tests for SlidingWindowStreamRedactor using Hypothesis.

Guarantees that regardless of how an upstream provider or network splits
chunks across SSE token boundaries (even 1 character at a time or split
right through a high-entropy secret), secrets NEVER leak to the downstream client.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.services.streaming_redactor import SlidingWindowStreamRedactor

SAMPLE_SECRETS = [
    "sk-proj-abc1234567890abcdefghijklmnopqrstuvwxyz",
    "ghp_" + "1234567890abcdefghijklmnopqrstuvwxyz",
    "AKIAIOSFODNN7EXAMPLE",
    "postgresql://postgres:SuperSecretP@ssw0rd!@db.internal.net:5432/prod",
    "mongodb://root:adminSecret123@cluster0.mongodb.net:27017/admin",
    "xox" + "b-1234567890-1234567890123-abcdefghijklmnopqrstuvwx",
    "AIzaSyD-1234567890abcdefghijklmnopqrst",
]


@st.composite
def text_with_secret_and_chunks(draw) -> tuple[str, list[str], str]:
    """Generate arbitrary text containing a secret and split it into random chunks."""
    prefix = draw(
        st.text(
            alphabet=st.characters(
                whitelist_categories=("L", "N", "P", "Z"), blacklist_characters=("\r",)
            ),
            min_size=0,
            max_size=100,
        )
    )
    secret = draw(st.sampled_from(SAMPLE_SECRETS))
    suffix = draw(
        st.text(
            alphabet=st.characters(
                whitelist_categories=("L", "N", "P", "Z"), blacklist_characters=("\r",)
            ),
            min_size=0,
            max_size=100,
        )
    )

    full_text = f"{prefix} {secret} {suffix}"

    # Generate random slice points to cut full_text into arbitrary chunks
    text_len = len(full_text)
    if text_len == 0:
        return full_text, [""], secret

    num_cuts = draw(st.integers(min_value=1, max_value=min(20, text_len)))
    cut_points = sorted(draw(st.lists(st.integers(min_value=1, max_value=text_len - 1), min_size=num_cuts, max_size=num_cuts, unique=True)))

    chunks = []
    prev = 0
    for cp in cut_points:
        chunks.append(full_text[prev:cp])
        prev = cp
    chunks.append(full_text[prev:])

    return full_text, chunks, secret


@settings(max_examples=500, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(text_with_secret_and_chunks())
def test_sliding_window_redacts_arbitrary_chunk_splits(data):
    """Property test: No raw secret ever appears in emitted stream chunks across arbitrary splits."""
    full_text, chunks, secret = data

    redactor = SlidingWindowStreamRedactor(lookahead_chars=64)
    emitted_chunks: list[str] = []

    for chunk in chunks:
        out = redactor.process_delta(chunk, is_final=False)
        if out:
            emitted_chunks.append(out)

    final_tail = redactor.process_delta("", is_final=True)
    if final_tail:
        emitted_chunks.append(final_tail)

    reassembled_output = "".join(emitted_chunks)

    # 1. Zero-Leak Property: The raw secret MUST NOT appear in the final emitted stream
    assert secret not in reassembled_output, (
        f"CRITICAL LEAK: Raw secret '{secret}' leaked in stream output!\n"
        f"Original full text: {full_text!r}\n"
        f"Chunks passed: {chunks!r}\n"
        f"Emitted stream: {reassembled_output!r}"
    )

    # 2. Redaction replacement property
    assert "__SG_SECRET_" in reassembled_output, (
        f"Secret was not replaced with placeholder.\n"
        f"Original: {full_text!r}\n"
        f"Emitted: {reassembled_output!r}"
    )


def test_char_by_char_split_redaction():
    """Deterministic extreme boundary test: Secret fed strictly 1 character per chunk."""
    secret = "sk-proj-abc1234567890abcdefghijklmnopqrstuvwxyz"
    full_text = f"API configuration: {secret} enabled."

    redactor = SlidingWindowStreamRedactor(lookahead_chars=64)
    emitted = []

    for ch in full_text:
        out = redactor.process_delta(ch, is_final=False)
        if out:
            emitted.append(out)

    tail = redactor.process_delta("", is_final=True)
    if tail:
        emitted.append(tail)

    result = "".join(emitted)
    assert secret not in result
    assert "__SG_SECRET_1__" in result
    assert "API configuration:" in result
    assert "enabled." in result
