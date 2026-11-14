from __future__ import annotations

import logging
import sqlite3
import time
from typing import Tuple

logger = logging.getLogger("sentinelgate.budget")


class BudgetManager:
    """Common-Sense Spend Circuit Breaker:

    Tracks daily USD spend and immediately halts upstream queries when budget is breached,
    protecting developers against runaway costs and rogue loops.
    """

    def __init__(self, db_path: str = "sentinelgate.db", daily_limit_usd: float = 10.0):
        self.db_path = db_path
        self.daily_limit_usd = daily_limit_usd
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_spend (
                    day_key TEXT PRIMARY KEY,
                    spend_usd REAL NOT NULL DEFAULT 0.0,
                    request_count INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
            """)
            conn.commit()

    @staticmethod
    def get_today_key() -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def get_today_spend(self) -> Tuple[float, int]:
        day_key = self.get_today_key()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT spend_usd, request_count FROM daily_spend WHERE day_key = ?",
                (day_key,),
            )
            row = cursor.fetchone()
            if row:
                return float(row[0]), int(row[1])
        return 0.0, 0

    def check_budget(self) -> Tuple[bool, float, float]:
        """Check if today's spend is within limit. Returns (is_allowed, current_spend, limit)."""
        spent, _ = self.get_today_spend()
        is_allowed = spent < self.daily_limit_usd
        return is_allowed, spent, self.daily_limit_usd

    def record_spend(self, cost_usd: float):
        if cost_usd <= 0:
            return
        day_key = self.get_today_key()
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO daily_spend (day_key, spend_usd, request_count, updated_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(day_key) DO UPDATE SET
                    spend_usd = spend_usd + excluded.spend_usd,
                    request_count = request_count + 1,
                    updated_at = excluded.updated_at
                """,
                (day_key, round(cost_usd, 6), now),
            )
            conn.commit()
