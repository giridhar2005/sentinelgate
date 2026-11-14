from __future__ import annotations

import json
import logging
import random
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger("sentinelgate.evals")


class EvalsManager:
    """Telemetry logging, performance metrics aggregation, and shadow evaluations."""

    def __init__(self, db_path: str = "sentinelgate.db", sample_rate: float = 0.10):
        self.db_path = db_path
        self.sample_rate = sample_rate

    def log_request(
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
        timing_json = json.dumps(timing_breakdown or {})
        with sqlite3.connect(self.db_path, timeout=15.0) as conn:
            conn.execute(
                """
                INSERT INTO request_metrics (
                    request_id, model, provider, latency_ms, cache_hit,
                    prompt_tokens, completion_tokens, cost_usd, cost_saved_usd, timestamp, ttft_ms,
                    status, prompt_snippet, similarity_score, timing_breakdown, response_snippet
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
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
                    timing_json,
                    response_snippet[:500],
                ),
            )

    def log_guardrail_event(self, request_id: str, event_type: str, threat_detail: str, blocked: bool):
        with sqlite3.connect(self.db_path, timeout=15.0) as conn:
            conn.execute(
                """
                INSERT INTO guardrail_events (request_id, event_type, threat_detail, blocked, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (request_id, event_type, threat_detail[:500], 1 if blocked else 0, time.time()),
            )

    def should_sample_shadow(self, cache_hit: bool) -> bool:
        return False if cache_hit else random.random() < self.sample_rate

    async def run_shadow_eval(
        self,
        router,
        request_id: str,
        original_model: str,
        messages: List[Dict[str, Any]],
        original_response_text: str,
        fallback_model: str = "groq/llama-3.1-8b-instant",
        judge_model: str = "gemini/gemini-1.5-flash",
    ):
        try:
            prompt_str = "\n".join([f"{m.get('role')}: {m.get('content')}" for m in messages])
            shadow_req = {"model": fallback_model, "messages": messages, "temperature": 0.0}
            shadow_resp, _, _ = await router.execute_request(shadow_req, stream=False)
            shadow_text = shadow_resp.get("choices", [{}])[0].get("message", {}).get("content", "")

            judge_prompt = (
                "You are an impartial judge evaluating LLM quality parity.\n"
                f"User Request:\n{prompt_str}\n\nReference Response ({original_model}):\n{original_response_text}\n\n"
                f"Candidate Response ({fallback_model}):\n{shadow_text}\n\n"
                "Rate quality parity from 1 to 10 (10 = identical/superior, >=7 = acceptable parity, <7 = degradation).\n"
                "Output strictly:\nScore: <1-10>\nReason: <1 concise sentence>"
            )
            judge_req = {"model": judge_model, "messages": [{"role": "user", "content": judge_prompt}], "temperature": 0.0}
            judge_resp, _, _ = await router.execute_request(judge_req, stream=False)
            judge_output = judge_resp.get("choices", [{}])[0].get("message", {}).get("content", "")

            score_match = re.search(r"Score:\s*(\d+)", judge_output)
            reason_match = re.search(r"Reason:\s*(.+)", judge_output)
            score = max(1, min(10, int(score_match.group(1)))) if score_match else 7
            reason = reason_match.group(1).strip() if reason_match else "Quality parity evaluation completed."
            is_win = 1 if score >= 7 else 0

            with sqlite3.connect(self.db_path, timeout=15.0) as conn:
                conn.execute(
                    """
                    INSERT INTO shadow_evals (
                        request_id, original_model, shadow_model, prompt, original_response,
                        shadow_response, judge_score, judge_reasoning, is_win, timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        original_model,
                        fallback_model,
                        prompt_str[:1000],
                        original_response_text[:1000],
                        shadow_text[:1000],
                        score,
                        reason[:500],
                        is_win,
                        time.time(),
                    ),
                )
        except Exception as e:
            logger.error(f"Shadow eval error: {e}")

    def get_metrics_summary(self) -> Dict[str, Any]:
        with sqlite3.connect(self.db_path, timeout=15.0) as conn:
            conn.row_factory = sqlite3.Row
            agg = dict(
                conn.execute("""
                SELECT COUNT(*) as total_requests, SUM(cache_hit) as cache_hits,
                       SUM(cost_usd) as total_cost, SUM(cost_saved_usd) as total_saved,
                       SUM(prompt_tokens) as total_prompt_tokens, SUM(completion_tokens) as total_completion_tokens
                FROM request_metrics
            """).fetchone()
                or {}
            )

            latencies = [
                r[0]
                for r in conn.execute(
                    "SELECT latency_ms FROM request_metrics ORDER BY latency_ms ASC"
                ).fetchall()
            ]
            p50 = float(np.percentile(latencies, 50)) if latencies else 0.0
            p95 = float(np.percentile(latencies, 95)) if latencies else 0.0
            avg_ttft = float(
                conn.execute("SELECT AVG(ttft_ms) FROM request_metrics WHERE ttft_ms > 0").fetchone()[0] or 0.0
            )

            shadow_agg = dict(
                conn.execute(
                    "SELECT COUNT(*) as total_evals, SUM(is_win) as total_wins, AVG(judge_score) as avg_score FROM shadow_evals"
                ).fetchone()
                or {}
            )

            recent_requests = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT request_id, model, provider, latency_ms, cache_hit, prompt_tokens,
                           completion_tokens, cost_usd, cost_saved_usd, timestamp, ttft_ms,
                           COALESCE(status, CASE WHEN cache_hit = 1 THEN 'CACHE_HIT' ELSE 'UPSTREAM' END) as status,
                           COALESCE(prompt_snippet, '') as prompt_snippet,
                           COALESCE(similarity_score, 0.0) as similarity_score,
                           COALESCE(timing_breakdown, '{}') as timing_breakdown,
                           COALESCE(response_snippet, '') as response_snippet
                    FROM request_metrics ORDER BY id DESC LIMIT 50
                    """
                ).fetchall()
            ]

            recent_shadows = [
                dict(r)
                for r in conn.execute(
                    "SELECT request_id, original_model, shadow_model, judge_score, judge_reasoning, is_win, timestamp, prompt, original_response, shadow_response FROM shadow_evals ORDER BY id DESC LIMIT 10"
                ).fetchall()
            ]

            guardrail_count = conn.execute("SELECT COUNT(*) FROM guardrail_events WHERE blocked = 1").fetchone()[0] or 0
            scrubbed_count = conn.execute("SELECT COUNT(*) FROM guardrail_events WHERE event_type = 'SECRET_REDACTED'").fetchone()[0] or 0

        total_req = agg.get("total_requests") or 0
        cache_hits = agg.get("cache_hits") or 0
        hit_rate = round((cache_hits / total_req * 100), 1) if total_req > 0 else 0.0

        total_evals = shadow_agg.get("total_evals") or 0
        wins = shadow_agg.get("total_wins") or 0
        win_rate = round((wins / total_evals * 100), 1) if total_evals > 0 else 0.0

        return {
            "total_requests": total_req,
            "cache_hits": cache_hits,
            "cache_hit_rate": hit_rate,
            "total_cost_usd": round(agg.get("total_cost") or 0.0, 6),
            "total_saved_usd": round(agg.get("total_saved") or 0.0, 6),
            "total_prompt_tokens": agg.get("total_prompt_tokens") or 0,
            "total_completion_tokens": agg.get("total_completion_tokens") or 0,
            "p50_latency_ms": round(p50, 1),
            "p95_latency_ms": round(p95, 1),
            "avg_ttft_ms": round(avg_ttft, 1),
            "blocked_guardrail_events": guardrail_count,
            "secrets_scrubbed": scrubbed_count,
            "shadow_evals": {
                "total_evals": total_evals,
                "total_wins": wins,
                "win_rate": win_rate,
                "avg_score": round(shadow_agg.get("avg_score") or 0.0, 2),
            },
            "recent_requests": recent_requests,
            "recent_shadow_evals": recent_shadows,
        }

    def export_golden_dataset(self, limit: int = 100) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path, timeout=15.0) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT original_model, shadow_model, prompt, original_response,
                       shadow_response, judge_score, judge_reasoning, is_win, timestamp
                FROM shadow_evals
                ORDER BY judge_score DESC, id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "input": r["prompt"],
                "reference": r["original_response"],
                "candidate": r["shadow_response"],
                "score": r["judge_score"],
                "reasoning": r["judge_reasoning"],
                "parity_win": bool(r["is_win"]),
                "models": {"primary": r["original_model"], "shadow": r["shadow_model"]},
                "timestamp": r["timestamp"],
            }
            for r in rows
        ]
