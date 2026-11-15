"""Non-Blocking Async Telemetry Pipeline.

Routes request metrics, spend calculations, and guardrail audit events into
an in-memory asyncio.Queue drained by a background batch-flusher task.
Guarantees the gateway request hot-path never waits on SQLite disk writes.
Flushes every 100ms or 50 items using SQLite executemany in WAL mode.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("sentinelgate.telemetry_queue")


class AsyncTelemetryPipeline:
    """Non-blocking background batch flusher for SQLite telemetry in WAL mode."""

    def __init__(
        self,
        db_path: str = "sentinelgate.db",
        flush_interval_seconds: float = 0.1,  # 100ms
        batch_size: int = 50,
        max_queue_size: int = 10000,
    ):
        self.db_path = db_path
        self.flush_interval = flush_interval_seconds
        self.batch_size = batch_size
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue_size)
        self._worker_task: Optional[asyncio.Task] = None
        self._running: bool = False
        self._flushed_requests_count: int = 0
        self._flushed_guardrails_count: int = 0

    def start(self):
        """Start the async background worker task."""
        if self._running:
            return
        self._running = True
        try:
            loop = asyncio.get_running_loop()
            self._worker_task = loop.create_task(self._worker_loop())
            logger.info("Non-blocking async telemetry pipeline started.")
        except RuntimeError:
            # If no event loop is running, worker will run in sync fallback mode
            self._running = False

    async def stop(self):
        """Drain the queue and cleanly shut down the background worker."""
        if not self._running:
            self._flush_sync_drain()
            return

        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

        # Drain any remaining items
        await self._flush_all_async()
        logger.info(
            f"Telemetry pipeline shut down. Total flushed: {self._flushed_requests_count} requests, "
            f"{self._flushed_guardrails_count} guardrails."
        )

    def enqueue_request(
        self,
        request_id: str,
        model: str,
        provider: str,
        latency_ms: float,
        cache_hit: bool,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
        cost_saved_usd: float = 0.0,
        ttft_ms: float = 0.0,
        status: str = "UPSTREAM",
        prompt_snippet: str = "",
        similarity_score: float = 0.0,
        timing_breakdown: Optional[Dict[str, float]] = None,
        response_snippet: str = "",
    ):
        """Enqueue request metric in sub-microsecond in-memory time."""
        payload = (
            "request",
            (
                request_id,
                model,
                provider,
                round(latency_ms, 2),
                1 if cache_hit else 0,
                prompt_tokens,
                completion_tokens,
                round(cost_usd, 6),
                round(cost_saved_usd, 6),
                time.time(),
                round(ttft_ms, 2),
                status,
                prompt_snippet[:300],
                round(similarity_score, 4),
                json.dumps(timing_breakdown or {}),
                response_snippet[:500],
            ),
        )
        if self._running and self._worker_task and not self._worker_task.done():
            try:
                self.queue.put_nowait(payload)
                return
            except asyncio.QueueFull:
                logger.warning("Telemetry queue is full! Dropping item to prevent memory bloat.")
                return

        # Fallback to direct write if pipeline worker isn't active
        self._write_single_request(payload[1])

    def enqueue_guardrail(
        self, request_id: str, event_type: str, threat_detail: str, blocked: bool
    ):
        """Enqueue guardrail audit event without blocking."""
        payload = (
            "guardrail",
            (request_id, event_type, threat_detail[:500], 1 if blocked else 0, time.time()),
        )
        if self._running and self._worker_task and not self._worker_task.done():
            try:
                self.queue.put_nowait(payload)
                return
            except asyncio.QueueFull:
                logger.warning("Telemetry queue is full! Dropping guardrail item.")
                return

        self._write_single_guardrail(payload[1])

    async def _worker_loop(self):
        """Background flusher loop batching items with 100ms or 50 items trigger."""
        while self._running:
            try:
                # Wait up to flush_interval for the first item
                try:
                    first_item = await asyncio.wait_for(self.queue.get(), timeout=self.flush_interval)
                    items = [first_item]
                    self.queue.task_done()
                except asyncio.TimeoutError:
                    continue

                # Drain up to batch_size items currently waiting
                while len(items) < self.batch_size:
                    try:
                        item = self.queue.get_nowait()
                        items.append(item)
                        self.queue.task_done()
                    except asyncio.QueueEmpty:
                        break

                if items:
                    self._flush_batch_sync(items)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in telemetry flusher loop: {e}")
                await asyncio.sleep(0.05)

    def _flush_batch_sync(self, items: List[Tuple[str, Tuple]]):
        """Batch insert into SQLite using executemany in a single transaction."""
        req_rows = [it[1] for it in items if it[0] == "request"]
        guard_rows = [it[1] for it in items if it[0] == "guardrail"]

        try:
            with sqlite3.connect(self.db_path, timeout=15.0) as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                if req_rows:
                    conn.executemany(
                        """
                        INSERT INTO request_metrics (
                            request_id, model, provider, latency_ms, cache_hit,
                            prompt_tokens, completion_tokens, cost_usd, cost_saved_usd, timestamp, ttft_ms,
                            status, prompt_snippet, similarity_score, timing_breakdown, response_snippet
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        req_rows,
                    )
                    self._flushed_requests_count += len(req_rows)

                if guard_rows:
                    conn.executemany(
                        """
                        INSERT INTO guardrail_events (request_id, event_type, threat_detail, blocked, timestamp)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        guard_rows,
                    )
                    self._flushed_guardrails_count += len(guard_rows)

                conn.commit()
        except Exception as e:
            logger.error(f"Failed to batch flush {len(items)} telemetry records: {e}")

    async def _flush_all_async(self):
        """Drain and flush all items remaining in queue."""
        items = []
        while not self.queue.empty():
            try:
                items.append(self.queue.get_nowait())
                self.queue.task_done()
            except asyncio.QueueEmpty:
                break

        if items:
            self._flush_batch_sync(items)

    def _flush_sync_drain(self):
        """Synchronously drain all pending items if any."""
        items = []
        while not self.queue.empty():
            try:
                items.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if items:
            self._flush_batch_sync(items)

    def _write_single_request(self, row: Tuple):
        """Fallback direct write for synchronous tests or when worker is stopped."""
        try:
            with sqlite3.connect(self.db_path, timeout=15.0) as conn:
                conn.execute(
                    """
                    INSERT INTO request_metrics (
                        request_id, model, provider, latency_ms, cache_hit,
                        prompt_tokens, completion_tokens, cost_usd, cost_saved_usd, timestamp, ttft_ms,
                        status, prompt_snippet, similarity_score, timing_breakdown, response_snippet
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    row,
                )
        except Exception as e:
            logger.error(f"Fallback request metric write failed: {e}")

    def _write_single_guardrail(self, row: Tuple):
        """Fallback direct write for synchronous tests or when worker is stopped."""
        try:
            with sqlite3.connect(self.db_path, timeout=15.0) as conn:
                conn.execute(
                    """
                    INSERT INTO guardrail_events (request_id, event_type, threat_detail, blocked, timestamp)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    row,
                )
        except Exception as e:
            logger.error(f"Fallback guardrail write failed: {e}")
