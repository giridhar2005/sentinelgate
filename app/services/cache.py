from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("sentinelgate.cache")


class SemanticCache:
    """Local, sub-15ms semantic caching engine powered by FastEmbed (ONNX) + SQLite WAL."""

    def __init__(
        self,
        db_path: str = "sentinelgate.db",
        similarity_threshold: float = 0.92,
        default_ttl: int = 3600,
    ):
        self.db_path = db_path
        self.similarity_threshold = similarity_threshold
        self.default_ttl = default_ttl
        self.embedding_model = None
        self._init_embedder()

    def _init_embedder(self):
        try:
            from fastembed import TextEmbedding

            self.embedding_model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
            logger.info("FastEmbed BAAI/bge-small-en-v1.5 ONNX runtime initialized.")
        except Exception as e:
            logger.warning(f"FastEmbed initialization deferred: {e}. Fallback to vector hashing.")

    def _get_embedding(self, text: str) -> np.ndarray:
        if self.embedding_model:
            try:
                embeddings = list(self.embedding_model.embed([text]))
                vec = np.array(embeddings[0], dtype=np.float32)
                norm = np.linalg.norm(vec)
                return vec / norm if norm > 0 else vec
            except Exception as e:
                logger.error(f"FastEmbed inference error: {e}")

        # Deterministic vector hash fallback
        vec = np.zeros(384, dtype=np.float32)
        words = text.lower().split()
        for w in words:
            h = int(hashlib.md5(w.encode("utf-8")).hexdigest(), 16)
            idx = h % 384
            vec[idx] += 1.0
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else vec

    def _extract_prompt(self, messages: List[Dict[str, Any]]) -> str:
        parts = []
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str):
                parts.append(f"{m.get('role', 'user')}: {content}")
        return "\n".join(parts)

    def lookup(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        bypass_header: Optional[str] = None,
        cache_control: Optional[str] = None,
    ) -> Optional[Tuple[Dict[str, Any], float]]:
        """Lookup cached response if similarity >= threshold and unexpired."""
        if bypass_header and bypass_header.lower() in ("true", "1"):
            return None
        if cache_control and "no-cache" in cache_control.lower():
            return None

        prompt_str = self._extract_prompt(messages)
        if not prompt_str:
            return None

        prompt_hash = hashlib.sha256(prompt_str.encode("utf-8")).hexdigest()
        now = time.time()

        with sqlite3.connect(self.db_path, timeout=15.0) as conn:
            conn.row_factory = sqlite3.Row
            # 1. Exact hash fast-path
            row = conn.execute(
                "SELECT response_json, expires_at FROM semantic_cache WHERE prompt_hash = ? AND model = ? AND expires_at > ?",
                (prompt_hash, model, now),
            ).fetchone()
            if row:
                resp = json.loads(row["response_json"])
                resp["sentinel_cache_hit"] = True
                resp["sentinel_similarity"] = 1.0
                return resp, 1.0

            # 2. Vectorized cosine similarity lookup across active unexpired entries
            candidates = conn.execute(
                "SELECT id, embedding, response_json, expires_at FROM semantic_cache WHERE model = ? AND expires_at > ?",
                (model, now),
            ).fetchall()

            if not candidates:
                return None

            query_vec = self._get_embedding(prompt_str)
            best_score = -1.0
            best_resp = None

            for c in candidates:
                vec_blob = c["embedding"]
                cached_vec = np.frombuffer(vec_blob, dtype=np.float32)
                sim = float(np.dot(query_vec, cached_vec))
                if sim > best_score:
                    best_score = sim
                    best_resp = c["response_json"]

            if best_score >= self.similarity_threshold and best_resp:
                resp = json.loads(best_resp)
                resp["sentinel_cache_hit"] = True
                resp["sentinel_similarity"] = round(best_score, 4)
                return resp, best_score

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
        """Store prompt embedding and response payload in SQLite."""
        prompt_str = self._extract_prompt(messages)
        if not prompt_str:
            return

        prompt_hash = hashlib.sha256(prompt_str.encode("utf-8")).hexdigest()
        vec = self._get_embedding(prompt_str)
        now = time.time()
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl
        expires_at = now + ttl

        resp_clean = dict(response)
        resp_clean.pop("sentinel_cache_hit", None)
        resp_clean.pop("sentinel_similarity", None)

        with sqlite3.connect(self.db_path, timeout=15.0) as conn:
            conn.execute(
                """
                INSERT INTO semantic_cache (
                    prompt_hash, prompt_text, embedding, response_json,
                    prompt_tokens, completion_tokens, created_at, expires_at, model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prompt_hash,
                    prompt_str[:1000],
                    vec.tobytes(),
                    json.dumps(resp_clean),
                    prompt_tokens,
                    completion_tokens,
                    now,
                    expires_at,
                    model,
                ),
            )
