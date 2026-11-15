from __future__ import annotations

import logging
import secrets
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("sentinelgate.budget")


class BudgetManager:
    """Spend Circuit Breaker: Tracks daily spend and halts upstream queries on breach."""

    def __init__(self, db_path: str = "sentinelgate.db", daily_limit_usd: float = 10.0):
        self.db_path = db_path
        self.daily_limit_usd = daily_limit_usd

    @staticmethod
    def get_today_key() -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def get_today_spend(self) -> Tuple[float, int]:
        with sqlite3.connect(self.db_path, timeout=15.0) as conn:
            row = conn.execute(
                "SELECT spend_usd, request_count FROM daily_spend WHERE day_key = ?",
                (self.get_today_key(),),
            ).fetchone()
            return (float(row[0]), int(row[1])) if row else (0.0, 0)

    def check_budget(self) -> Tuple[bool, float, float]:
        spent, _ = self.get_today_spend()
        return spent < self.daily_limit_usd, spent, self.daily_limit_usd

    def record_spend(self, cost_usd: float):
        if cost_usd <= 0:
            return
        with sqlite3.connect(self.db_path, timeout=15.0) as conn:
            conn.execute(
                """
                INSERT INTO daily_spend (day_key, spend_usd, request_count, updated_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(day_key) DO UPDATE SET
                    spend_usd = spend_usd + excluded.spend_usd,
                    request_count = request_count + 1, updated_at = excluded.updated_at
                """,
                (self.get_today_key(), round(cost_usd, 6), time.time()),
            )


class VirtualKeyManager:
    """Enterprise Virtual Key Manager & Dual-Bucket Sliding-Window Rate Limiter (RPM + TPM)."""

    def __init__(self, db_path: str = "sentinelgate.db"):
        self.db_path = db_path

    def create_key(
        self,
        name: str,
        daily_budget_usd: float = 10.0,
        monthly_budget_usd: float = 300.0,
        rate_limit_rpm: int = 60,
        rate_limit_tpm: int = 60000,
    ) -> Dict[str, Any]:
        clean = "".join(c for c in name.lower() if c.isalnum() or c in "-_")[:12] or "app"
        key_val = f"sg-live-{clean}-{secrets.token_hex(6)}"
        now = time.time()
        with sqlite3.connect(self.db_path, timeout=15.0) as conn:
            conn.execute(
                """
                INSERT INTO virtual_keys (key_val, name, daily_budget_usd, monthly_budget_usd, rate_limit_rpm, rate_limit_tpm, created_at, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    key_val,
                    name,
                    daily_budget_usd,
                    monthly_budget_usd,
                    rate_limit_rpm,
                    rate_limit_tpm,
                    now,
                ),
            )
        return {
            "key": key_val,
            "name": name,
            "daily_budget_usd": daily_budget_usd,
            "monthly_budget_usd": monthly_budget_usd,
            "rate_limit_rpm": rate_limit_rpm,
            "rate_limit_tpm": rate_limit_tpm,
            "created_at": now,
        }

    def list_keys(self) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path, timeout=15.0) as conn:
            conn.row_factory = sqlite3.Row
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM virtual_keys ORDER BY created_at DESC"
                ).fetchall()
            ]

    def revoke_key(self, key_val: str) -> bool:
        with sqlite3.connect(self.db_path, timeout=15.0) as conn:
            cursor = conn.execute(
                "UPDATE virtual_keys SET is_active = 0 WHERE key_val = ?", (key_val,)
            )
            return cursor.rowcount > 0

    def verify_and_rate_limit(
        self, key_val: str, estimated_tokens: int = 0
    ) -> Tuple[bool, Optional[str], Dict[str, str]]:
        now = time.time()
        minute = int(now // 60)
        reset_seconds = max(1, int(60 - (now % 60)))
        default_headers: Dict[str, str] = {}

        with sqlite3.connect(self.db_path, timeout=15.0) as conn:
            row = conn.execute(
                "SELECT name, rate_limit_rpm, COALESCE(rate_limit_tpm, 60000), is_active FROM virtual_keys WHERE key_val = ?",
                (key_val,),
            ).fetchone()
            if not row:
                return False, "Invalid virtual gateway key", default_headers

            name, rpm, tpm, is_active = row[0], int(row[1]), int(row[2]), bool(row[3])
            if not is_active:
                return False, f"Virtual key '{name}' is revoked", default_headers

            rl = conn.execute(
                "SELECT request_count, COALESCE(token_count, 0) FROM key_rate_limits WHERE key_val = ? AND window_minute = ?",
                (key_val, minute),
            ).fetchone()
            cnt = int(rl[0]) if rl else 0
            tok_cnt = int(rl[1]) if rl else 0

            headers = {
                "X-RateLimit-Limit-Requests": str(rpm),
                "X-RateLimit-Remaining-Requests": str(max(0, rpm - (cnt + 1))),
                "X-RateLimit-Reset-Requests": str(reset_seconds),
                "X-RateLimit-Limit-Tokens": str(tpm),
                "X-RateLimit-Remaining-Tokens": str(max(0, tpm - (tok_cnt + estimated_tokens))),
                "X-RateLimit-Reset-Tokens": str(reset_seconds),
            }

            if cnt >= rpm:
                return (
                    False,
                    f"Rate limit exceeded for virtual key '{name}': {rpm} RPM limit",
                    headers,
                )

            if (tok_cnt + estimated_tokens) > tpm:
                return (
                    False,
                    f"Rate limit exceeded for virtual key '{name}': {tpm} TPM limit (current {tok_cnt} + requested {estimated_tokens})",
                    headers,
                )

            conn.execute(
                """
                INSERT INTO key_rate_limits (key_val, window_minute, request_count, token_count)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(key_val, window_minute) DO UPDATE SET
                    request_count = request_count + 1,
                    token_count = token_count + excluded.token_count
                """,
                (key_val, minute, estimated_tokens),
            )
            return True, None, headers

    def record_token_adjustment(self, key_val: str, token_delta: int):
        """Adjust token consumption delta after exact response usage is known."""
        if token_delta == 0:
            return
        now = time.time()
        minute = int(now // 60)
        with sqlite3.connect(self.db_path, timeout=15.0) as conn:
            conn.execute(
                """
                INSERT INTO key_rate_limits (key_val, window_minute, request_count, token_count)
                VALUES (?, ?, 0, ?)
                ON CONFLICT(key_val, window_minute) DO UPDATE SET
                    token_count = MAX(0, token_count + excluded.token_count)
                """,
                (key_val, minute, token_delta),
            )
