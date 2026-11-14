from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("sentinelgate.cache")


class SemanticCache:
    """Semantic Cache using FastEmbed (ONNX) and NumPy cosine similarity backed by SQLite."""

    def __init__(
        self,
        db_path: str = "sentinelgate.db",
        similarity_threshold: float = 0.92,
        ttl_seconds: int = 3600,
        embedding_fn: Optional[Callable[[str], np.ndarray]] = None,
    ):
        self.db_path = db_path
        self.similarity_threshold = similarity_threshold
        self.ttl_seconds = ttl_seconds
        self._embedder = None
        self._custom_embed_fn = embedding_fn
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS semantic_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, model TEXT NOT NULL,
                    prompt TEXT NOT NULL, embedding BLOB NOT NULL,
                    response_json TEXT NOT NULL, prompt_tokens INTEGER DEFAULT 0,
                    completion_tokens INTEGER DEFAULT 0, created_at REAL NOT NULL,
                    ttl_seconds INTEGER DEFAULT 3600
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_created ON semantic_cache(created_at);")

    def _get_embedder(self):
        if self._embedder is None:
            try:
                from fastembed import TextEmbedding
                self._embedder = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
            except Exception as e:
                logger.warning(f"FastEmbed init note: {e}")
        return self._embedder

    def embed_text(self, text: str) -> np.ndarray:
        if self._custom_embed_fn:
            return self._custom_embed_fn(text)
        embedder = self._get_embedder()
        if embedder:
            try:
                vec = np.array(list(embedder.embed([text]))[0], dtype=np.float32)
                norm = np.linalg.norm(vec)
                return vec / (norm if norm > 0 else 1.0)
            except Exception as e:
                logger.error(f"Embedding failed: {e}")
        h = np.zeros(384, dtype=np.float32)
        for i, c in enumerate(text.lower().strip()):
            h[ord(c) % 384] += 1.0 / (i + 1.0)
        norm = np.linalg.norm(h)
        return h / (norm if norm > 0 else 1.0)

    @staticmethod
    def extract_prompt_text(messages: List[Dict[str, Any]]) -> str:
        return "\n".join([f"{m.get('role', '')}: {m.get('content', '')}" for m in messages]).strip()

    def lookup(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        bypass_header: Optional[str] = None,
    ) -> Optional[Tuple[Dict[str, Any], float]]:
        if bypass_header and bypass_header.strip().lower() in ("true", "1", "yes"):
            return None
        prompt_text = self.extract_prompt_text(messages)
        if not prompt_text:
            return None

        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT id, prompt, embedding, response_json FROM semantic_cache WHERE (created_at + ttl_seconds) > ? ORDER BY id DESC LIMIT 500",
                (now,),
            ).fetchall()

        if not rows:
            return None

        query_vec = self.embed_text(prompt_text)
        matrix = np.frombuffer(b"".join([r[2] for r in rows]), dtype=np.float32).reshape(len(rows), -1)
        dots = np.dot(matrix, query_vec)
        norms = np.linalg.norm(matrix, axis=1) * np.linalg.norm(query_vec)
        similarities = dots / np.maximum(norms, 1e-9)

        best_idx = int(np.argmax(similarities))
        best_score = float(similarities[best_idx])

        if best_score >= self.similarity_threshold:
            try:
                cached_resp = json.loads(rows[best_idx][3])
                cached_resp["sentinel_cache_hit"] = True
                cached_resp["sentinel_similarity"] = round(best_score, 4)
                return cached_resp, best_score
            except Exception:
                return None
        return None

    def store(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        response: Dict[str, Any],
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        ttl_seconds: Optional[int] = None,
    ):
        prompt_text = self.extract_prompt_text(messages)
        if not prompt_text or not response:
            return
        vector = self.embed_text(prompt_text)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO semantic_cache (
                    model, prompt, embedding, response_json, prompt_tokens,
                    completion_tokens, created_at, ttl_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (model, prompt_text, vector.tobytes(), json.dumps(response), prompt_tokens, completion_tokens, time.time(), ttl_seconds or self.ttl_seconds),
            )
