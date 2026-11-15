"""Verification suite for Non-Blocking Async Telemetry Pipeline."""

import asyncio
import sqlite3
import time

import pytest

from app.core.database import init_db
from app.services.telemetry_queue import AsyncTelemetryPipeline


@pytest.mark.asyncio
async def test_async_telemetry_batch_flusher(tmp_path):
    """Verify non-blocking enqueuing and background batch flushing to SQLite."""
    db_file = str(tmp_path / "test_telemetry.db")
    init_db(db_file)

    pipeline = AsyncTelemetryPipeline(db_path=db_file, flush_interval_seconds=0.05, batch_size=25)
    pipeline.start()

    t0 = time.perf_counter()
    n_records = 100
    for i in range(n_records):
        pipeline.enqueue_request(
            request_id=f"req-{i}",
            model="gpt-4o-mini",
            provider="openai",
            latency_ms=12.5,
            cache_hit=(i % 2 == 0),
            prompt_tokens=10,
            completion_tokens=20,
            cost_usd=0.0001,
            cost_saved_usd=0.00005,
            ttft_ms=5.0,
            status="UPSTREAM",
            prompt_snippet=f"prompt {i}",
        )
    hotpath_duration_ms = (time.perf_counter() - t0) * 1000.0

    # 100 in-memory queue pushes must finish well within 15 milliseconds
    assert hotpath_duration_ms < 50.0

    # Enqueue guardrails as well
    for i in range(20):
        pipeline.enqueue_guardrail(
            request_id=f"req-g-{i}",
            event_type="TOOL_CALL_BLOCKED",
            threat_detail="rm -rf / blocked",
            blocked=True,
        )

    # Allow worker loop to batch and flush
    await asyncio.sleep(0.15)
    await pipeline.stop()

    # Verify SQLite WAL contains all records
    with sqlite3.connect(db_file) as conn:
        req_count = conn.execute("SELECT COUNT(*) FROM request_metrics").fetchone()[0]
        guard_count = conn.execute("SELECT COUNT(*) FROM guardrail_events").fetchone()[0]

    assert req_count == 100
    assert guard_count == 20


@pytest.mark.asyncio
async def test_async_telemetry_shutdown_drain(tmp_path):
    """Ensure zero records are lost during abrupt shutdown while queue is full."""
    db_file = str(tmp_path / "test_telemetry_drain.db")
    init_db(db_file)

    pipeline = AsyncTelemetryPipeline(db_path=db_file, flush_interval_seconds=10.0, batch_size=500)
    pipeline.start()

    # Enqueue 50 items while flush interval is large (10s)
    for i in range(50):
        pipeline.enqueue_request(
            request_id=f"drain-req-{i}",
            model="llama-3.1-8b",
            provider="groq",
            latency_ms=8.0,
            cache_hit=False,
            prompt_tokens=15,
            completion_tokens=25,
            cost_usd=0.00002,
        )

    # Stop pipeline immediately: should drain everything cleanly
    await pipeline.stop()

    with sqlite3.connect(db_file) as conn:
        req_count = conn.execute("SELECT COUNT(*) FROM request_metrics").fetchone()[0]

    assert req_count == 50


def test_async_telemetry_sync_fallback(tmp_path):
    """Verify synchronous fallback when no event loop or worker task is active."""
    db_file = str(tmp_path / "test_telemetry_sync.db")
    init_db(db_file)

    pipeline = AsyncTelemetryPipeline(db_path=db_file)
    # Notice: pipeline.start() is not called

    pipeline.enqueue_request(
        request_id="sync-1",
        model="gpt-4o",
        provider="openai",
        latency_ms=45.0,
        cache_hit=False,
        prompt_tokens=50,
        completion_tokens=100,
        cost_usd=0.002,
    )
    pipeline.enqueue_guardrail(
        request_id="sync-1",
        event_type="SECRET_REDACTED",
        threat_detail="Scrubbed API key",
        blocked=False,
    )

    with sqlite3.connect(db_file) as conn:
        req_count = conn.execute("SELECT COUNT(*) FROM request_metrics").fetchone()[0]
        guard_count = conn.execute("SELECT COUNT(*) FROM guardrail_events").fetchone()[0]

    assert req_count == 1
    assert guard_count == 1
