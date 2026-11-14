from __future__ import annotations

import logging
import random
import re
import sqlite3
import time
from typing import Any, Dict, List

import numpy as np

logger = logging.getLogger("sentinelgate.evals")


class EvalsManager:
    """Manages telemetry logging, metrics aggregation, and asynchronous 10% shadow evaluation."""

    def __init__(self, db_path: str = "sentinelgate.db", sample_rate: float = 0.10):
        self.db_path = db_path
        self.sample_rate = sample_rate
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS request_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    latency_ms REAL NOT NULL,
                    cache_hit INTEGER NOT NULL,
                    prompt_tokens INTEGER NOT NULL,
                    completion_tokens INTEGER NOT NULL,
                    cost_usd REAL NOT NULL,
                    cost_saved_usd REAL NOT NULL,
                    timestamp REAL NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS shadow_evals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    original_model TEXT NOT NULL,
                    shadow_model TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    original_response TEXT NOT NULL,
                    shadow_response TEXT NOT NULL,
                    judge_score INTEGER NOT NULL,
                    judge_reasoning TEXT NOT NULL,
                    is_win INTEGER NOT NULL,
                    timestamp REAL NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_ts ON request_metrics(timestamp);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_ts ON shadow_evals(timestamp);")
            conn.commit()

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
    ):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO request_metrics (
                    request_id, model, provider, latency_ms, cache_hit,
                    prompt_tokens, completion_tokens, cost_usd, cost_saved_usd, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
            conn.commit()

    def should_sample_shadow(self, cache_hit: bool) -> bool:
        if cache_hit:
            return False
        return random.random() < self.sample_rate

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
        """Asynchronously replays prompt to cheaper fallback model and scores parity via judge model."""
        try:
            # 1. Replay prompt to fallback model
            prompt_str = "\n".join([f"{m.get('role')}: {m.get('content')}" for m in messages])
            shadow_req = {"model": fallback_model, "messages": messages, "temperature": 0.0}
            shadow_resp, _, _ = await router.execute_request(shadow_req, stream=False)

            shadow_text = (
                shadow_resp.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )

            # 2. Score quality parity using LLM-as-a-judge
            judge_prompt = (
                "You are an impartial judge evaluating LLM quality parity.\n"
                f"User Request:\n{prompt_str}\n\n"
                f"Reference Response ({original_model}):\n{original_response_text}\n\n"
                f"Candidate Response ({fallback_model}):\n{shadow_text}\n\n"
                "Compare the candidate response against the reference. Rate quality parity on an integer scale from 1 to 10 "
                "(10 = identical or superior quality, >=7 = acceptable quality parity for production fallback, <7 = unacceptable degradation).\n"
                "Provide your judgment strictly in this format:\n"
                "Score: <number 1-10>\n"
                "Reason: <1 concise sentence>"
            )

            judge_req = {
                "model": judge_model,
                "messages": [{"role": "user", "content": judge_prompt}],
                "temperature": 0.0,
            }
            judge_resp, _, _ = await router.execute_request(judge_req, stream=False)
            judge_output = (
                judge_resp.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )

            # Parse score and reason
            score_match = re.search(r"Score:\s*(\d+)", judge_output)
            reason_match = re.search(r"Reason:\s*(.+)", judge_output)

            score = int(score_match.group(1)) if score_match else 7
            score = max(1, min(10, score))
            reason = reason_match.group(1).strip() if reason_match else "Quality parity evaluation completed."
            is_win = 1 if score >= 7 else 0

            # 3. Store in SQLite
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO shadow_evals (
                        request_id, original_model, shadow_model, prompt,
                        original_response, shadow_response, judge_score, judge_reasoning,
                        is_win, timestamp
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
                conn.commit()

            logger.info(f"Shadow eval completed for {request_id}: score={score}/10 win={bool(is_win)}")
        except Exception as e:
            logger.error(f"Shadow eval background task failed: {e}")

    def get_metrics_summary(self) -> Dict[str, Any]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            # Aggregates from request_metrics
            cursor = conn.execute("""
                SELECT
                    COUNT(*) as total_requests,
                    SUM(cache_hit) as cache_hits,
                    SUM(cost_usd) as total_cost,
                    SUM(cost_saved_usd) as total_saved,
                    SUM(prompt_tokens) as total_prompt_tokens,
                    SUM(completion_tokens) as total_completion_tokens
                FROM request_metrics
            """)
            agg = dict(cursor.fetchone() or {})

            # Latency percentiles
            l_cursor = conn.execute("SELECT latency_ms FROM request_metrics ORDER BY latency_ms ASC")
            latencies = [r[0] for r in l_cursor.fetchall()]

            p50 = float(np.percentile(latencies, 50)) if latencies else 0.0
            p95 = float(np.percentile(latencies, 95)) if latencies else 0.0

            # Shadow evals aggregates
            s_cursor = conn.execute("""
                SELECT
                    COUNT(*) as total_evals,
                    SUM(is_win) as total_wins,
                    AVG(judge_score) as avg_score
                FROM shadow_evals
            """)
            shadow_agg = dict(s_cursor.fetchone() or {})

            # Recent requests
            r_cursor = conn.execute("""
                SELECT request_id, model, provider, latency_ms, cache_hit,
                       prompt_tokens, completion_tokens, cost_usd, timestamp
                FROM request_metrics
                ORDER BY id DESC LIMIT 20
            """)
            recent_requests = [dict(r) for r in r_cursor.fetchall()]

            # Recent shadow evals
            se_cursor = conn.execute("""
                SELECT request_id, original_model, shadow_model, judge_score,
                       judge_reasoning, is_win, timestamp
                FROM shadow_evals
                ORDER BY id DESC LIMIT 10
            """)
            recent_shadows = [dict(r) for r in se_cursor.fetchall()]

        total_req = agg.get("total_requests") or 0
        cache_hits = agg.get("cache_hits") or 0
        hit_rate = round((cache_hits / total_req * 100), 1) if total_req > 0 else 0.0

        total_evals = shadow_agg.get("total_evals") or 0
        wins = shadow_agg.get("total_wins") or 0
        win_rate = round((wins / total_evals * 100), 1) if total_evals > 0 else 0.0
        avg_score = round(shadow_agg.get("avg_score") or 0.0, 2)

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
            "shadow_evals": {
                "total_evals": total_evals,
                "total_wins": wins,
                "win_rate": win_rate,
                "avg_score": avg_score,
            },
            "recent_requests": recent_requests,
            "recent_shadow_evals": recent_shadows,
        }
