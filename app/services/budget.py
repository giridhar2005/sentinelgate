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
    """Enterprise Virtual Key Manager & Sliding-Window Rate Limiter."""

    def __init__(self, db_path: str = "sentinelgate.db"):
        self.db_path = db_path

    def create_key(
        self,
        name: str,
        daily_budget_usd: float = 10.0,
        monthly_budget_usd: float = 300.0,
        rate_limit_rpm: int = 60,
    ) -> Dict[str, Any]:
        clean = "".join(c for c in name.lower() if c.isalnum() or c in "-_")[:12] or "app"
        key_val = f"sg-live-{clean}-{secrets.token_hex(6)}"
        now = time.time()
        with sqlite3.connect(self.db_path, timeout=15.0) as conn:
            conn.execute(
                "INSERT INTO virtual_keys VALUES (?, ?, ?, ?, ?, ?, 1)",
                (key_val, name, daily_budget_usd, monthly_budget_usd, rate_limit_rpm, now),
            )
        return {
            "key": key_val,
            "name": name,
            "daily_budget_usd": daily_budget_usd,
            "monthly_budget_usd": monthly_budget_usd,
            "rate_limit_rpm": rate_limit_rpm,
            "created_at": now,
        }

    def list_keys(self) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path, timeout=15.0) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM virtual_keys ORDER BY created_at DESC").fetchall()]

    def verify_and_rate_limit(self, key_val: str) -> Tuple[bool, Optional[str]]:
        now = time.time()
        minute = int(now // 60)
        with sqlite3.connect(self.db_path, timeout=15.0) as conn:
            row = conn.execute(
                "SELECT name, rate_limit_rpm, is_active FROM virtual_keys WHERE key_val = ?",
                (key_val,),
            ).fetchone()
            if not row:
                return False, "Invalid virtual gateway key"
            name, rpm, is_active = row[0], row[1], row[2]
            if not is_active:
                return False, f"Virtual key '{name}' is revoked"

            rl = conn.execute(
                "SELECT request_count FROM key_rate_limits WHERE key_val = ? AND window_minute = ?",
                (key_val, minute),
            ).fetchone()
            cnt = rl[0] if rl else 0
            if cnt >= rpm:
                return False, f"Rate limit exceeded for virtual key '{name}': {rpm} rpm limit"

            conn.execute(
                """
                INSERT INTO key_rate_limits (key_val, window_minute, request_count) VALUES (?, ?, 1)
                ON CONFLICT(key_val, window_minute) DO UPDATE SET request_count = request_count + 1
                """,
                (key_val, minute),
            )
            return True, None
