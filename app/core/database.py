from __future__ import annotations

import contextlib
import logging
import sqlite3
from typing import Generator

logger = logging.getLogger("sentinelgate.database")


def init_db(db_path: str = "sentinelgate.db"):
    """Initialize SQLite database with WAL mode and core schemas."""
    with sqlite3.connect(db_path, timeout=30.0, check_same_thread=False) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA mmap_size=30000000000;")
        conn.execute("PRAGMA cache_size=-64000;")

        cursor = conn.cursor()
        # Schema migration check: ensure semantic_cache has prompt_hash
        cursor.execute("PRAGMA table_info(semantic_cache);")
        cache_cols = {row[1] for row in cursor.fetchall()}
        if cache_cols and "prompt_hash" not in cache_cols:
            logger.warning("Migrating legacy semantic_cache schema...")
            conn.execute("DROP TABLE semantic_cache;")

        # 1. Semantic Cache Table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS semantic_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt_hash TEXT NOT NULL,
                prompt_text TEXT NOT NULL,
                embedding BLOB NOT NULL,
                response_json TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL,
                completion_tokens INTEGER NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                model TEXT NOT NULL
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_hash ON semantic_cache(prompt_hash);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_expires ON semantic_cache(expires_at);")

        # 2. Telemetry Request Metrics Table
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
                timestamp REAL NOT NULL,
                ttft_ms REAL DEFAULT 0.0,
                status TEXT DEFAULT 'UPSTREAM',
                prompt_snippet TEXT DEFAULT '',
                similarity_score REAL DEFAULT 0.0,
                timing_breakdown TEXT DEFAULT '{}',
                response_snippet TEXT DEFAULT ''
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_ts ON request_metrics(timestamp);")

        cursor.execute("PRAGMA table_info(request_metrics);")
        rm_cols = {row[1] for row in cursor.fetchall()}
        for col_name, col_type in [
            ("status", "TEXT DEFAULT 'UPSTREAM'"),
            ("prompt_snippet", "TEXT DEFAULT ''"),
            ("similarity_score", "REAL DEFAULT 0.0"),
            ("timing_breakdown", "TEXT DEFAULT '{}'"),
            ("response_snippet", "TEXT DEFAULT ''"),
        ]:
            if col_name not in rm_cols:
                conn.execute(f"ALTER TABLE request_metrics ADD COLUMN {col_name} {col_type};")

        # 3. Continuous Shadow Parity Evals Table
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

        # 4. Daily Spend Circuit Breaker Table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_spend (
                day_key TEXT PRIMARY KEY,
                spend_usd REAL DEFAULT 0.0,
                request_count INTEGER DEFAULT 0,
                updated_at REAL NOT NULL
            );
        """)

        # 5. Multi-Tenant Virtual Keys Table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS virtual_keys (
                key_val TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                daily_budget_usd REAL DEFAULT 10.0,
                monthly_budget_usd REAL DEFAULT 300.0,
                rate_limit_rpm INTEGER DEFAULT 60,
                created_at REAL NOT NULL,
                is_active INTEGER DEFAULT 1
            );
        """)

        # 6. Sliding-Window Rate Limiting Table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS key_rate_limits (
                key_val TEXT NOT NULL,
                window_minute INTEGER NOT NULL,
                request_count INTEGER DEFAULT 0,
                PRIMARY KEY(key_val, window_minute)
            );
        """)

        # 7. Security & Guardrail Audit Log Table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guardrail_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                threat_detail TEXT NOT NULL,
                blocked INTEGER NOT NULL,
                timestamp REAL NOT NULL
            );
        """)


@contextlib.contextmanager
def get_db(db_path: str = "sentinelgate.db") -> Generator[sqlite3.Connection, None, None]:
    """Yield a connection with WAL mode enabled and Row factory."""
    conn = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
