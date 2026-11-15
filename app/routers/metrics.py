from __future__ import annotations

import sqlite3
from typing import List

from fastapi import APIRouter, Request, Response

router = APIRouter(tags=["Prometheus Metrics"])


def escape_label(val: str) -> str:
    return str(val).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


@router.get("/metrics")
async def prometheus_metrics(request: Request):
    """Serve real-time operational telemetry in standard Prometheus exposition format (RFC 0004)."""
    evals = request.app.state.evals
    gateway_router = request.app.state.router
    db_path = evals.db_path

    lines: List[str] = [
        "# HELP sentinelgate_up Gateway process health and availability status (1 = healthy).",
        "# TYPE sentinelgate_up gauge",
        "sentinelgate_up 1",
        "",
    ]

    # Query metrics from SQLite WAL
    with sqlite3.connect(db_path, timeout=10.0) as conn:
        conn.row_factory = sqlite3.Row

        # 1. Total Requests by model, provider, status
        req_counts = conn.execute(
            """
            SELECT model, provider, status, COUNT(*) as count
            FROM request_metrics
            GROUP BY model, provider, status
            """
        ).fetchall()

        lines.append(
            "# HELP sentinelgate_requests_total Cumulative count of requests processed by the gateway."
        )
        lines.append("# TYPE sentinelgate_requests_total counter")
        for r in req_counts:
            m = escape_label(r["model"])
            p = escape_label(r["provider"])
            s = escape_label(r["status"])
            lines.append(
                f'sentinelgate_requests_total{{model="{m}",provider="{p}",status="{s}"}} {r["count"]}'
            )
        if not req_counts:
            lines.append(
                'sentinelgate_requests_total{model="none",provider="none",status="none"} 0'
            )
        lines.append("")

        # 2. Token consumption
        tok_counts = conn.execute(
            """
            SELECT SUM(prompt_tokens) as prompt_tokens, SUM(completion_tokens) as completion_tokens
            FROM request_metrics
            """
        ).fetchone()

        p_tok = tok_counts["prompt_tokens"] or 0
        c_tok = tok_counts["completion_tokens"] or 0

        lines.append(
            "# HELP sentinelgate_tokens_total Cumulative input and output LLM tokens routed."
        )
        lines.append("# TYPE sentinelgate_tokens_total counter")
        lines.append(f'sentinelgate_tokens_total{{type="prompt"}} {p_tok}')
        lines.append(f'sentinelgate_tokens_total{{type="completion"}} {c_tok}')
        lines.append("")

        # 3. Cache Hits vs Misses
        cache_agg = conn.execute(
            """
            SELECT SUM(cache_hit) as hits, COUNT(*) as total
            FROM request_metrics
            """
        ).fetchone()
        total_r = cache_agg["total"] or 0
        hits = cache_agg["hits"] or 0
        misses = max(0, total_r - hits)

        lines.append(
            "# HELP sentinelgate_cache_lookups_total Total semantic cache operations segmented by result."
        )
        lines.append("# TYPE sentinelgate_cache_lookups_total counter")
        lines.append(f'sentinelgate_cache_lookups_total{{result="hit"}} {hits}')
        lines.append(f'sentinelgate_cache_lookups_total{{result="miss"}} {misses}')
        lines.append("")

        # 4. Latency Distribution Histogram (seconds)
        lat_rows = [
            r[0] / 1000.0 for r in conn.execute("SELECT latency_ms FROM request_metrics").fetchall()
        ]
        buckets = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]

        lines.append(
            "# HELP sentinelgate_request_duration_seconds End-to-end request duration histogram in seconds."
        )
        lines.append("# TYPE sentinelgate_request_duration_seconds histogram")
        for b in buckets:
            cnt = sum(1 for lat in lat_rows if lat <= b)
            lines.append(f'sentinelgate_request_duration_seconds_bucket{{le="{b}"}} {cnt}')
        lines.append(f'sentinelgate_request_duration_seconds_bucket{{le="+Inf"}} {len(lat_rows)}')
        lines.append(f"sentinelgate_request_duration_seconds_sum {sum(lat_rows):.4f}")
        lines.append(f"sentinelgate_request_duration_seconds_count {len(lat_rows)}")
        lines.append("")

        # 5. Financial Economics
        econ_agg = conn.execute(
            """
            SELECT SUM(cost_usd) as spent, SUM(cost_saved_usd) as saved
            FROM request_metrics
            """
        ).fetchone()
        spent = float(econ_agg["spent"] or 0.0)
        saved = float(econ_agg["saved"] or 0.0)

        lines.append(
            "# HELP sentinelgate_cost_spent_usd_total Cumulative USD spent on upstream provider APIs."
        )
        lines.append("# TYPE sentinelgate_cost_spent_usd_total counter")
        lines.append(f"sentinelgate_cost_spent_usd_total {spent:.6f}")
        lines.append("")

        lines.append(
            "# HELP sentinelgate_cost_saved_usd_total Cumulative USD saved via semantic cache and model routing."
        )
        lines.append("# TYPE sentinelgate_cost_saved_usd_total counter")
        lines.append(f"sentinelgate_cost_saved_usd_total {saved:.6f}")
        lines.append("")

        # 6. Guardrails & Secrets Scrubbed
        scrub_cnt = (
            conn.execute(
                "SELECT COUNT(*) FROM guardrail_events WHERE event_type = 'SECRET_REDACTED'"
            ).fetchone()[0]
            or 0
        )
        block_cnt = (
            conn.execute("SELECT COUNT(*) FROM guardrail_events WHERE blocked = 1").fetchone()[0]
            or 0
        )

        lines.append(
            "# HELP sentinelgate_secrets_scrubbed_total Total credentials scrubbed by Secret Firewall."
        )
        lines.append("# TYPE sentinelgate_secrets_scrubbed_total counter")
        lines.append(f"sentinelgate_secrets_scrubbed_total {scrub_cnt}")
        lines.append("")

        lines.append(
            "# HELP sentinelgate_guardrail_blocked_total Total requests blocked by security guardrails."
        )
        lines.append("# TYPE sentinelgate_guardrail_blocked_total counter")
        lines.append(f"sentinelgate_guardrail_blocked_total {block_cnt}")
        lines.append("")

        # 7. Virtual Keys Active
        active_keys = (
            conn.execute("SELECT COUNT(*) FROM virtual_keys WHERE is_active = 1").fetchone()[0] or 0
        )

        lines.append(
            "# HELP sentinelgate_active_virtual_keys Current number of active tenant virtual keys."
        )
        lines.append("# TYPE sentinelgate_active_virtual_keys gauge")
        lines.append(f"sentinelgate_active_virtual_keys {active_keys}")
        lines.append("")

    # 8. Speculative Hedging Telemetry
    hedge_trig = getattr(gateway_router, "hedged_triggered_count", 0)
    hedge_pri = getattr(gateway_router, "hedged_primary_won_count", 0)
    hedge_sec = getattr(gateway_router, "hedged_secondary_won_count", 0)

    lines.append(
        "# HELP sentinelgate_hedged_requests_total Speculative latency hedging executions and race outcomes."
    )
    lines.append("# TYPE sentinelgate_hedged_requests_total counter")
    lines.append(f'sentinelgate_hedged_requests_total{{outcome="triggered"}} {hedge_trig}')
    lines.append(f'sentinelgate_hedged_requests_total{{outcome="primary_won"}} {hedge_pri}')
    lines.append(f'sentinelgate_hedged_requests_total{{outcome="hedge_won"}} {hedge_sec}')
    lines.append("")

    # 9. Circuit Breaker States per Provider
    cb = getattr(gateway_router, "circuit_breaker", None)
    lines.append(
        "# HELP sentinelgate_circuit_breaker_tripped Total times circuit breaker tripped per provider."
    )
    lines.append("# TYPE sentinelgate_circuit_breaker_tripped counter")
    if cb and hasattr(cb, "trip_counts"):
        for prov, trips in cb.trip_counts.items():
            lines.append(
                f'sentinelgate_circuit_breaker_tripped{{provider="{escape_label(prov)}"}} {trips}'
            )
    else:
        for prov in gateway_router.providers:
            lines.append(
                f'sentinelgate_circuit_breaker_tripped{{provider="{escape_label(prov)}"}} 0'
            )
    lines.append("")

    body = "\n".join(lines) + "\n"
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")
