"""Zero-Dependency OpenTelemetry (OTLP/HTTP JSON) Exporter & W3C traceparent Handler.

Provides standard OpenTelemetry trace and span creation without requiring
the 50MB opentelemetry-sdk dependency tree. Emits W3C-compliant traceparent headers
and dispatches OTLP JSON trace payloads to standard collectors (Jaeger, Grafana Tempo,
Datadog, OpenTelemetry Collector).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
from collections import deque
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("sentinelgate.otel")

W3C_TRACEPARENT_REGEX = re.compile(
    r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$", re.IGNORECASE
)


class TraceContext:
    """W3C Trace Context representation (RFC 0004 / W3C Recommendation)."""

    def __init__(
        self,
        trace_id: str,
        span_id: str,
        trace_flags: str = "01",
        parent_span_id: Optional[str] = None,
    ):
        self.trace_id = trace_id.lower()
        self.span_id = span_id.lower()
        self.trace_flags = trace_flags.lower()
        self.parent_span_id = parent_span_id.lower() if parent_span_id else None

    @classmethod
    def from_header(cls, header_val: Optional[str] = None) -> TraceContext:
        """Parse incoming traceparent header or generate a new trace context."""
        if header_val:
            m = W3C_TRACEPARENT_REGEX.match(header_val.strip())
            if m:
                incoming_trace_id = m.group(1)
                incoming_parent_id = m.group(2)
                flags = m.group(3)
                # Generate a child span for this gateway hop
                current_span_id = secrets.token_hex(8)
                return cls(
                    trace_id=incoming_trace_id,
                    span_id=current_span_id,
                    trace_flags=flags,
                    parent_span_id=incoming_parent_id,
                )

        # Generate fresh trace and span IDs
        return cls(
            trace_id=secrets.token_hex(16),
            span_id=secrets.token_hex(8),
            trace_flags="01",
            parent_span_id=None,
        )

    def to_header(self) -> str:
        """Serialize to standard W3C traceparent format."""
        return f"00-{self.trace_id}-{self.span_id}-{self.trace_flags}"

    def create_child(self) -> TraceContext:
        """Derive a child trace context for downstream hops."""
        return TraceContext(
            trace_id=self.trace_id,
            span_id=secrets.token_hex(8),
            trace_flags=self.trace_flags,
            parent_span_id=self.span_id,
        )


class OTLPSpan:
    """Zero-dependency OpenTelemetry Span data holder."""

    def __init__(
        self,
        name: str,
        trace_id: str,
        span_id: str,
        parent_span_id: Optional[str] = None,
        start_time_ns: Optional[int] = None,
        end_time_ns: Optional[int] = None,
        attributes: Optional[Dict[str, Any]] = None,
        status_code: int = 1,  # 1 = OK, 2 = ERROR, 0 = UNSET
        status_message: str = "",
    ):
        self.name = name
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        now_ns = int(time.time() * 1e9)
        self.start_time_ns = start_time_ns or now_ns
        self.end_time_ns = end_time_ns or now_ns
        self.attributes = attributes or {}
        self.status_code = status_code
        self.status_message = status_message

    def finish(self, status_code: Optional[int] = None, status_message: Optional[str] = None):
        """Set span end timestamp and optional status."""
        self.end_time_ns = int(time.time() * 1e9)
        if status_code is not None:
            self.status_code = status_code
        if status_message is not None:
            self.status_message = status_message

    def to_dict(self) -> Dict[str, Any]:
        """Convert to OpenTelemetry v1 OTLP JSON dictionary."""
        kv_attrs = []
        for k, v in self.attributes.items():
            val_node: Dict[str, Any]
            if isinstance(v, bool):
                val_node = {"boolValue": v}
            elif isinstance(v, int):
                val_node = {"intValue": str(v)}
            elif isinstance(v, float):
                val_node = {"doubleValue": float(v)}
            else:
                val_node = {"stringValue": str(v)}
            kv_attrs.append({"key": str(k), "value": val_node})

        span_dict: Dict[str, Any] = {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "name": self.name,
            "kind": 2,  # SPAN_KIND_SERVER
            "startTimeUnixNano": str(self.start_time_ns),
            "endTimeUnixNano": str(self.end_time_ns),
            "attributes": kv_attrs,
            "status": {
                "code": self.status_code,
            },
        }
        if self.parent_span_id:
            span_dict["parentSpanId"] = self.parent_span_id
        if self.status_message:
            span_dict["status"]["message"] = self.status_message
        return span_dict


class OTLPExporter:
    """Dispatches OTLP JSON spans to HTTP endpoints and stores recent spans in-memory."""

    def __init__(self, endpoint: Optional[str] = None, max_ring_buffer: int = 200):
        # Allow env override
        self.endpoint = (
            endpoint
            or os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
            or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        )
        if self.endpoint and not self.endpoint.endswith("/v1/traces"):
            # If standard base URL like http://localhost:4318, append /v1/traces
            if self.endpoint.endswith("/"):
                self.endpoint = f"{self.endpoint}v1/traces"
            else:
                self.endpoint = f"{self.endpoint}/v1/traces"

        self._ring_buffer: deque = deque(maxlen=max_ring_buffer)

    def record_span(self, span: OTLPSpan):
        """Append span to ring buffer and schedule async OTLP export if endpoint configured."""
        span_payload = span.to_dict()
        self._ring_buffer.append(span_payload)

        if self.endpoint:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._send_otlp_batch([span_payload]))
            except RuntimeError:
                # No active event loop in current thread
                pass

    async def _send_otlp_batch(self, spans: List[Dict[str, Any]]):
        """Send OTLP HTTP/JSON trace payload to collector."""
        if not self.endpoint:
            return

        payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": "sentinelgate"}},
                            {"key": "service.version", "value": {"stringValue": "0.1.0"}},
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "sentinelgate.tracer", "version": "0.1.0"},
                            "spans": spans,
                        }
                    ],
                }
            ]
        }

        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                res = await client.post(
                    self.endpoint,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                if res.status_code >= 400:
                    logger.warning(
                        f"OTLP Collector returned HTTP {res.status_code}: {res.text[:120]}"
                    )
        except Exception as e:
            logger.debug(f"Failed to dispatch OTLP spans to {self.endpoint}: {e}")

    def get_recent_spans(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return the most recent spans from the in-memory ring buffer."""
        items = list(self._ring_buffer)
        return items[-limit:]
