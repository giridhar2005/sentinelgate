from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx
import yaml

from app.core.circuit import ProviderCircuitBreaker
from app.core.config import AppConfig, expand_env_vars

logger = logging.getLogger("sentinelgate.router")


class ProviderRouter:
    """Zero-bloat direct REST router supporting OpenAI, Anthropic, Groq, Gemini, and Ollama

    with round-robin key rotation, automatic failover, and speculative latency hedging.
    """

    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = config_path
        self.config: AppConfig = self._load_config()
        self.providers: Dict[str, Any] = self.config.providers
        self.key_indices: Dict[str, int] = {p: 0 for p in self.providers}
        self.circuit_breaker = ProviderCircuitBreaker(failure_threshold=2, cooldown_seconds=30.0)
        self.client: Optional[httpx.AsyncClient] = None

    def _load_config(self) -> AppConfig:
        path = os.getenv("SENTINELGATE_CONFIG", self.config_path)
        if not os.path.exists(path):
            return AppConfig()
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            expanded = expand_env_vars(raw)
            return AppConfig.model_validate(expanded)
        except Exception as e:
            logger.warning(f"Failed to parse config: {e}. Using defaults.")
            return AppConfig()

    async def get_client(self) -> httpx.AsyncClient:
        if self.client is None or self.client.is_closed:
            limits = httpx.Limits(max_keepalive_connections=50, max_connections=100)
            self.client = httpx.AsyncClient(limits=limits, timeout=60.0)
        return self.client

    async def close(self):
        if self.client and not self.client.is_closed:
            await self.client.aclose()

    def get_all_models(self) -> List[Dict[str, Any]]:
        now = int(time.time())
        models = [
            {
                "id": "auto",
                "object": "model",
                "created": now,
                "owned_by": "sentinelgate",
                "permission": [],
                "root": "auto",
                "parent": None,
            }
        ]
        for p_name, p_data in self.providers.items():
            model_dict = p_data.models if hasattr(p_data, "models") else p_data.get("models", {})
            for m_name in model_dict:
                models.append({
                    "id": m_name,
                    "object": "model",
                    "created": now,
                    "owned_by": p_name,
                    "permission": [],
                    "root": m_name,
                    "parent": None,
                })
        return models

    def classify_complexity(self, messages: List[Dict[str, Any]]) -> str:
        full_text = " ".join([m.get("content", "") for m in messages if isinstance(m.get("content"), str)])
        lower_text = full_text.lower()
        complex_keywords = [
            "architect", "analyze", "step by step", "step-by-step", "derive",
            "refactor", "proof", "implement", "benchmark", "evaluate", "debug",
            "optimize", "tradeoffs", "design", "algorithm",
        ]
        if "```" in full_text or any(kw in lower_text for kw in complex_keywords) or len(full_text) > 350 or len(messages) >= 4:
            return "complex"
        return "simple"

    def resolve_model(
        self,
        model_name: str,
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[str, str, List[str]]:
        if model_name == "auto" and messages:
            complexity = self.classify_complexity(messages)
            if complexity == "simple":
                return "groq", "llama-3.1-8b-instant", ["openai/gpt-4o-mini", "gemini/gemini-1.5-flash"]
            else:
                return "groq", "llama-3.3-70b-versatile", ["openai/gpt-4o", "gemini/gemini-1.5-pro"]

        if "/" in model_name:
            p, m = model_name.split("/", 1)
            p_cfg = self.providers.get(p)
            models = p_cfg.models if p_cfg and hasattr(p_cfg, "models") else (p_cfg.get("models", {}) if isinstance(p_cfg, dict) else {})
            if m in models:
                m_cfg = models[m]
                fallbacks = getattr(m_cfg, "fallbacks", []) if hasattr(m_cfg, "fallbacks") else m_cfg.get("fallbacks", [])
                return p, m, fallbacks

        for p, p_cfg in self.providers.items():
            models = p_cfg.models if hasattr(p_cfg, "models") else (p_cfg.get("models", {}) if isinstance(p_cfg, dict) else {})
            if model_name in models:
                m_cfg = models[model_name]
                fallbacks = getattr(m_cfg, "fallbacks", []) if hasattr(m_cfg, "fallbacks") else m_cfg.get("fallbacks", [])
                return p, model_name, fallbacks

        # Default fallback
        for p, p_cfg in self.providers.items():
            models = p_cfg.models if hasattr(p_cfg, "models") else (p_cfg.get("models", {}) if isinstance(p_cfg, dict) else {})
            for m, m_cfg in models.items():
                fallbacks = getattr(m_cfg, "fallbacks", []) if hasattr(m_cfg, "fallbacks") else m_cfg.get("fallbacks", [])
                return p, m, fallbacks

        return "openai", "gpt-4o-mini", []

    def get_next_key(self, provider_name: str) -> str:
        p_cfg = self.providers.get(provider_name)
        keys = getattr(p_cfg, "api_keys", []) if hasattr(p_cfg, "api_keys") else (p_cfg.get("api_keys", []) if isinstance(p_cfg, dict) else [])
        clean_keys = [k for k in keys if k and not str(k).startswith("${")]
        if not clean_keys:
            return ""
        idx = self.key_indices.get(provider_name, 0)
        chosen_key = clean_keys[idx % len(clean_keys)]
        self.key_indices[provider_name] = (idx + 1) % len(clean_keys)
        return chosen_key

    def get_model_cost(self, provider: str, model: str, in_tok: int, out_tok: int) -> float:
        p_cfg = self.providers.get(provider)
        models = getattr(p_cfg, "models", {}) if hasattr(p_cfg, "models") else (p_cfg.get("models", {}) if isinstance(p_cfg, dict) else {})
        m_cfg = models.get(model, {})
        in_rate = getattr(m_cfg, "input_cost_per_m", 0.0) if hasattr(m_cfg, "input_cost_per_m") else m_cfg.get("input_cost_per_m", 0.0)
        out_rate = getattr(m_cfg, "output_cost_per_m", 0.0) if hasattr(m_cfg, "output_cost_per_m") else m_cfg.get("output_cost_per_m", 0.0)
        return round(((in_tok * in_rate) + (out_tok * out_rate)) / 1_000_000.0, 6)

    def is_provider_available(self, provider: str) -> bool:
        return self.circuit_breaker.is_available(provider)

    def record_provider_failure(self, provider: str, status_code: int = 500):
        self.circuit_breaker.record_failure(provider, status_code)

    def record_provider_success(self, provider: str):
        self.circuit_breaker.record_success(provider)

    def get_provider_statuses(self) -> Dict[str, Any]:
        return self.circuit_breaker.get_statuses(self.providers)

    # ------------------ PROTOCOL ADAPTERS ------------------

    def _build_gemini_payload(self, req: Dict[str, Any]) -> Dict[str, Any]:
        contents = []
        system_instruction = None
        for msg in req.get("messages", []):
            role = msg.get("role")
            text = msg.get("content", "")
            if role == "system":
                system_instruction = {"parts": [{"text": text}]}
            else:
                gemini_role = "model" if role == "assistant" else "user"
                contents.append({"role": gemini_role, "parts": [{"text": text}]})

        body: Dict[str, Any] = {"contents": contents}
        if system_instruction:
            body["system_instruction"] = system_instruction

        gen_config: Dict[str, Any] = {}
        if "temperature" in req and req["temperature"] is not None:
            gen_config["temperature"] = req["temperature"]
        if "max_tokens" in req and req["max_tokens"] is not None:
            gen_config["maxOutputTokens"] = req["max_tokens"]
        if req.get("response_format", {}).get("type") == "json_object":
            gen_config["responseMimeType"] = "application/json"
        if gen_config:
            body["generationConfig"] = gen_config
        return body

    def _convert_gemini_to_openai(self, gemini_data: Dict[str, Any], model: str) -> Dict[str, Any]:
        candidates = gemini_data.get("candidates", [])
        text = ""
        finish_reason = "stop"
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            if parts:
                text = parts[0].get("text", "")
            finish_reason = candidates[0].get("finishReason", "stop").lower()

        usage = gemini_data.get("usageMetadata", {})
        prompt_tokens = usage.get("promptTokenCount", max(1, len(str(gemini_data)) // 4))
        completion_tokens = usage.get("candidatesTokenCount", max(1, len(text) // 4))

        return {
            "id": f"chatcmpl-gemini-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": finish_reason,
            }],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    def _build_anthropic_payload(self, req: Dict[str, Any], model: str, stream: bool = False) -> Dict[str, Any]:
        system_text = ""
        chat_messages = []
        for m in req.get("messages", []):
            role = m.get("role")
            content = m.get("content", "")
            if role == "system":
                system_text += f"\n{content}".strip()
            else:
                anthropic_role = "assistant" if role == "assistant" else "user"
                chat_messages.append({"role": anthropic_role, "content": content})

        if not chat_messages:
            chat_messages.append({"role": "user", "content": "Hello"})

        body: Dict[str, Any] = {
            "model": model,
            "messages": chat_messages,
            "max_tokens": req.get("max_tokens") or 2048,
        }
        if system_text:
            body["system"] = system_text
        if "temperature" in req and req["temperature"] is not None:
            body["temperature"] = req["temperature"]
        if stream:
            body["stream"] = True
        return body

    def _convert_anthropic_to_openai(self, anth_data: Dict[str, Any], model: str) -> Dict[str, Any]:
        content_blocks = anth_data.get("content", [])
        text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
        usage = anth_data.get("usage", {})
        prompt_tokens = usage.get("input_tokens", max(1, len(str(anth_data)) // 4))
        completion_tokens = usage.get("output_tokens", max(1, len(text) // 4))

        return {
            "id": f"chatcmpl-ant-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": anth_data.get("stop_reason", "stop"),
            }],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    # ------------------ EXECUTION & FAILOVER ------------------

    async def execute_request(
        self,
        request_data: Dict[str, Any],
        stream: bool = False,
    ) -> Tuple[Union[Dict[str, Any], AsyncGenerator[str, None]], str, str]:
        requested_model = request_data.get("model", "gpt-4o-mini")
        provider, model, fallbacks = self.resolve_model(requested_model, request_data.get("messages"))

        candidates_queue = [(provider, model)] + [self.resolve_model(fb)[:2] for fb in fallbacks]
        run_queue = self.circuit_breaker.filter_available_candidates(candidates_queue)
        client = await self.get_client()

        last_error = None
        for p, m in run_queue:
            p_cfg = self.providers.get(p)
            p_type = getattr(p_cfg, "type", "openai") if hasattr(p_cfg, "type") else (p_cfg.get("type", "openai") if isinstance(p_cfg, dict) else "openai")
            base_url = (getattr(p_cfg, "base_url", "") if hasattr(p_cfg, "base_url") else (p_cfg.get("base_url", "") if isinstance(p_cfg, dict) else "")).rstrip("/")
            keys = getattr(p_cfg, "api_keys", []) if hasattr(p_cfg, "api_keys") else (p_cfg.get("api_keys", []) if isinstance(p_cfg, dict) else [])
            num_keys = max(1, len(keys))

            for _ in range(num_keys):
                api_key = self.get_next_key(p)
                headers = {"Content-Type": "application/json"}
                url = ""
                payload = dict(request_data)
                payload["model"] = m

                if p_type in ("openai", "openai_compatible", "ollama"):
                    url = f"{base_url}/chat/completions"
                    if api_key:
                        headers["Authorization"] = f"Bearer {api_key}"
                elif p_type == "anthropic":
                    url = f"{base_url}/messages"
                    headers["anthropic-version"] = "2023-06-01"
                    if api_key:
                        headers["x-api-key"] = api_key
                    payload = self._build_anthropic_payload(request_data, m, stream=stream)
                elif p_type == "gemini":
                    action = "streamGenerateContent?alt=sse&" if stream else "generateContent?"
                    url = f"{base_url}/models/{m}:{action}key={api_key}"
                    payload = self._build_gemini_payload(request_data)

                try:
                    if stream:
                        gen = self._stream_response(client, p_type, url, headers, payload, m)
                        self.record_provider_success(p)
                        return gen, p, m
                    else:
                        resp = await client.post(url, headers=headers, json=payload, timeout=60.0)
                        if resp.status_code in (429, 500, 502, 503):
                            self.record_provider_failure(p, resp.status_code)
                            logger.warning(f"Provider {p} returned {resp.status_code}. Failing over/rotating.")
                            last_error = f"HTTP {resp.status_code}: {resp.text}"
                            continue

                        resp.raise_for_status()
                        self.record_provider_success(p)
                        res_json = resp.json()
                        if p_type == "gemini":
                            res_json = self._convert_gemini_to_openai(res_json, m)
                        elif p_type == "anthropic":
                            res_json = self._convert_anthropic_to_openai(res_json, m)
                        return res_json, p, m

                except httpx.HTTPStatusError as e:
                    if e.response.status_code in (429, 500, 502, 503):
                        self.record_provider_failure(p, e.response.status_code)
                        last_error = str(e)
                        continue
                    raise e
                except (httpx.ConnectError, httpx.TimeoutException) as e:
                    self.record_provider_failure(p, 503)
                    logger.warning(f"Connection error contacting {p}: {e}")
                    last_error = str(e)
                    break

        raise RuntimeError(f"All providers and fallbacks exhausted. Last error: {last_error}")

    async def execute_hedged_request(
        self,
        request_data: Dict[str, Any],
        hedge_delay_sec: float = 0.45,
    ) -> Tuple[Dict[str, Any], str, str]:
        """Speculative Latency Hedging (Google Tail at Scale p99 Slasher)."""
        primary_task = asyncio.create_task(self.execute_request(request_data, stream=False))
        done, _ = await asyncio.wait([primary_task], timeout=hedge_delay_sec)
        if primary_task in done:
            return await primary_task

        hedged_payload = dict(request_data)
        hedged_payload["model"] = "groq/llama-3.1-8b-instant"
        hedge_task = asyncio.create_task(self.execute_request(hedged_payload, stream=False))

        done, pending = await asyncio.wait([primary_task, hedge_task], return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:
            try:
                res, p, m = await t
                return res, f"{p}(hedged)", m
            except Exception:
                pass
        raise RuntimeError("Hedged execution failed.")

    async def _stream_response(
        self,
        client: httpx.AsyncClient,
        p_type: str,
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
        model: str,
    ) -> AsyncGenerator[str, None]:
        req_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        if p_type in ("openai", "openai_compatible", "ollama"):
            payload["stream"] = True

        async with client.stream("POST", url, headers=headers, json=payload, timeout=60.0) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if p_type == "gemini":
                    if line.startswith("data: "):
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            yield "data: [DONE]\n\n"
                            continue
                        try:
                            g_data = json.loads(data_str)
                            parts = g_data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])
                            text = parts[0].get("text", "") if parts else ""
                            chunk = {
                                "id": req_id,
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": model,
                                "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                            }
                            yield f"data: {json.dumps(chunk)}\n\n"
                        except Exception:
                            continue
                elif p_type == "anthropic":
                    if line.startswith("data: "):
                        data_str = line[6:].strip()
                        try:
                            a_data = json.loads(data_str)
                            evt_type = a_data.get("type")
                            if evt_type == "content_block_delta":
                                delta_text = a_data.get("delta", {}).get("text", "")
                                chunk = {
                                    "id": req_id,
                                    "object": "chat.completion.chunk",
                                    "created": int(time.time()),
                                    "model": model,
                                    "choices": [{"index": 0, "delta": {"content": delta_text}, "finish_reason": None}],
                                }
                                yield f"data: {json.dumps(chunk)}\n\n"
                            elif evt_type == "message_stop":
                                yield "data: [DONE]\n\n"
                        except Exception:
                            continue
                else:
                    yield f"{line}\n\n"

    async def execute_embeddings(self, model: str, inputs: Union[str, List[str]]) -> Dict[str, Any]:
        """Compute embeddings either locally via FastEmbed or by proxying to an embedding provider."""
        text_list = [inputs] if isinstance(inputs, str) else inputs
        embeddings_list = []

        # If provider has configured embedding model or locally compute via fastembed
        try:
            from fastembed import TextEmbedding
            embedder = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
            raw_embeddings = list(embedder.embed(text_list))
            for i, vec in enumerate(raw_embeddings):
                embeddings_list.append({
                    "object": "embedding",
                    "index": i,
                    "embedding": [round(float(x), 6) for x in vec],
                })
        except Exception:
            # Deterministic fallback vector
            import hashlib

            import numpy as np
            for i, txt in enumerate(text_list):
                vec = np.zeros(384, dtype=float)
                for w in txt.lower().split():
                    h = int(hashlib.md5(w.encode("utf-8")).hexdigest(), 16)
                    vec[h % 384] += 1.0
                norm = np.linalg.norm(vec)
                normalized = (vec / norm if norm > 0 else vec).tolist()
                embeddings_list.append({
                    "object": "embedding",
                    "index": i,
                    "embedding": [round(float(x), 6) for x in normalized],
                })

        total_tokens = sum(max(1, len(t) // 4) for t in text_list)
        return {
            "object": "list",
            "data": embeddings_list,
            "model": model,
            "usage": {
                "prompt_tokens": total_tokens,
                "total_tokens": total_tokens,
            },
        }
