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
    """Local, multi-tenant isolated caching engine powered by FastEmbed (ONNX) + SQLite WAL.

    Degrades cleanly to exact-match-only cache when FastEmbed/ONNX is offline or unavailable.
    """

    def __init__(
        self,
        db_path: str = "sentinelgate.db",
        similarity_threshold: float = 0.92,
        default_ttl: int = 3600,
        exact_match_fallback: bool = True,
    ):
        self.db_path = db_path
        self.similarity_threshold = similarity_threshold
        self.default_ttl = default_ttl
        self.exact_match_fallback = exact_match_fallback
        self.embedding_model: Optional[Any] = None
        self.model_status: str = "uninitialized"
        self._init_embedder()

    def _init_embedder(self) -> None:
        try:
            from fastembed import TextEmbedding

            self.embedding_model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
            self.model_status = "ready"
            logger.info("FastEmbed BAAI/bge-small-en-v1.5 ONNX runtime initialized.")
        except Exception as e:
            logger.critical(
                f"FastEmbed ONNX initialization failed: {e}. Semantic embedding disabled."
            )
            if not self.exact_match_fallback:
                self.model_status = "failed"
                raise RuntimeError(
                    f"FastEmbed ONNX failed to initialize and exact_match_fallback is False: {e}"
                ) from e
            self.embedding_model = None
            self.model_status = "degraded_exact_match_only"

    @property
    def is_semantic_enabled(self) -> bool:
        return self.embedding_model is not None

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
            elif isinstance(content, list):
                # Handle structured content blocks
                text_parts = [
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                parts.append(f"{m.get('role', 'user')}: {' '.join(text_parts)}")
        return "\n".join(parts)

    def compute_key_components(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tenant_id: str = "default",
        system_prompt: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = 0.7,
    ) -> Tuple[str, str, str, str, str, float]:
        """Compute tenant-isolated composite cache key parameters."""
        prompt_str = self._extract_prompt(messages)
        prompt_hash = hashlib.sha256(prompt_str.encode("utf-8")).hexdigest() if prompt_str else ""
        sys_hash = (
            hashlib.sha256(system_prompt.encode("utf-8")).hexdigest() if system_prompt else "none"
        )
        tools_hash = (
            hashlib.sha256(json.dumps(tools or [], sort_keys=True).encode("utf-8")).hexdigest()
            if tools
            else "none"
        )
        rounded_temp = round(float(temperature if temperature is not None else 0.7), 2)
        return tenant_id, model, sys_hash, tools_hash, prompt_hash, rounded_temp

    def lookup(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tenant_id: str = "default",
        system_prompt: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = 0.7,
        bypass_header: Optional[str] = None,
        cache_control: Optional[str] = None,
    ) -> Optional[Tuple[Dict[str, Any], float]]:
        """Lookup cached response ensuring tenant isolation and composite key match."""
        if bypass_header and bypass_header.lower() in ("true", "1"):
            return None
        if cache_control and "no-cache" in cache_control.lower():
            return None

        prompt_str = self._extract_prompt(messages)
        if not prompt_str:
            return None

        (
            t_id,
            m_id,
            sys_hash,
            tools_hash,
            prompt_hash,
            rounded_temp,
        ) = self.compute_key_components(
            messages=messages,
            model=model,
            tenant_id=tenant_id,
            system_prompt=system_prompt,
            tools=tools,
            temperature=temperature,
        )

        now = time.time()

        with sqlite3.connect(self.db_path, timeout=15.0) as conn:
            conn.row_factory = sqlite3.Row
            # 1. Exact hash fast-path with multi-tenant isolation
            row = conn.execute(
                """
                SELECT response_json, expires_at FROM semantic_cache
                WHERE tenant_id = ? AND model = ? AND system_hash = ? AND tools_hash = ? AND temperature = ? AND prompt_hash = ? AND expires_at > ?
                """,
                (t_id, m_id, sys_hash, tools_hash, rounded_temp, prompt_hash, now),
            ).fetchone()
            if row:
                resp = json.loads(row["response_json"])
                resp["sentinel_cache_hit"] = True
                resp["sentinel_similarity"] = 1.0
                return resp, 1.0

            # 2. Vectorized cosine similarity lookup (only when semantic matching is enabled)
            if not self.is_semantic_enabled:
                return None

            candidates = conn.execute(
                """
                SELECT id, embedding, response_json, expires_at FROM semantic_cache
                WHERE tenant_id = ? AND model = ? AND system_hash = ? AND tools_hash = ? AND temperature = ? AND expires_at > ?
                """,
                (t_id, m_id, sys_hash, tools_hash, rounded_temp, now),
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
        tenant_id: str = "default",
        system_prompt: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = 0.7,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        ttl_seconds: Optional[int] = None,
    ) -> bool:
        """Store prompt embedding and response payload. Responses containing tool calls are NEVER cached."""
        # Check if response contains tool calls (stateful actions must never be cached)
        choices = response.get("choices", [])
        if choices:
            msg = choices[0].get("message", {})
            if msg.get("tool_calls") or msg.get("function_call"):
                logger.debug("Skipping cache store: response contains stateful tool calls.")
                return False

        content = response.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    logger.debug(
                        "Skipping cache store: response contains stateful Anthropic tool_use."
                    )
                    return False

        prompt_str = self._extract_prompt(messages)
        if not prompt_str:
            return False

        (
            t_id,
            m_id,
            sys_hash,
            tools_hash,
            prompt_hash,
            rounded_temp,
        ) = self.compute_key_components(
            messages=messages,
            model=model,
            tenant_id=tenant_id,
            system_prompt=system_prompt,
            tools=tools,
            temperature=temperature,
        )

        vec = (
            self._get_embedding(prompt_str)
            if self.is_semantic_enabled
            else np.zeros(384, dtype=np.float32)
        )
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
                    tenant_id, model, system_hash, tools_hash, temperature,
                    prompt_hash, prompt_text, embedding, response_json,
                    prompt_tokens, completion_tokens, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    t_id,
                    m_id,
                    sys_hash,
                    tools_hash,
                    rounded_temp,
                    prompt_hash,
                    prompt_str[:1000],
                    vec.tobytes(),
                    json.dumps(resp_clean),
                    prompt_tokens,
                    completion_tokens,
                    now,
                    expires_at,
                ),
            )
        return True
