from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx
import yaml

logger = logging.getLogger("sentinelgate.router")


class ProviderRouter:
    """Zero-bloat direct REST router supporting OpenAI, Groq, Gemini, and Ollama

    with round-robin key rotation and 429/503 automatic failover.
    """

    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = config_path
        self.config = self._load_config()
        self.providers: Dict[str, Any] = self.config.get("providers", {})
        self.key_indices: Dict[str, int] = {p: 0 for p in self.providers}
        self.client: Optional[httpx.AsyncClient] = None

    def _expand_env_vars(self, val: Any) -> Any:
        if isinstance(val, str):
            pattern = re.compile(r"\$\{([^}]+)\}")
            matches = pattern.findall(val)
            for m in matches:
                env_val = os.getenv(m, "")
                val = val.replace(f"${{{m}}}", env_val)
            return val
        elif isinstance(val, list):
            return [self._expand_env_vars(x) for x in val]
        elif isinstance(val, dict):
            return {k: self._expand_env_vars(v) for k, v in val.items()}
        return val

    def _load_config(self) -> Dict[str, Any]:
        if not os.path.exists(self.config_path):
            return {}
        with open(self.config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return self._expand_env_vars(raw)

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
            for m_name in p_data.get("models", {}):
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
        """Common-sense heuristic prompt classifier for adaptive tiering:

        - 'simple': Greetings, factual lookups, brief single-turn questions (< 120 chars) -> Tier 1.
        - 'complex': Reasoning keywords ('derive', 'architect', 'analyze', 'step-by-step', 'debug', 'code', 'compare', 'refactor'),
          code snippets (```), multi-step dialogue (>3 turns), or long prompt (> 400 chars) -> Tier 2.
        """
        full_text = " ".join([m.get("content", "") for m in messages if isinstance(m.get("content"), str)])
        lower_text = full_text.lower()

        complex_keywords = [
            "architect", "analyze", "step by step", "step-by-step", "derive",
            "refactor", "proof", "implement", "benchmark", "evaluate", "debug",
            "optimize", "tradeoffs", "design", "algorithm",
        ]
        has_code = "```" in full_text
        has_complex_keyword = any(kw in lower_text for kw in complex_keywords)
        is_long = len(full_text) > 350
        is_multi_turn = len(messages) >= 4

        if has_code or has_complex_keyword or is_long or is_multi_turn:
            return "complex"
        return "simple"

    def resolve_model(
        self,
        model_name: str,
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[str, str, List[str]]:
        """Resolve model name to (provider_name, internal_model_name, fallback_list)

        Supports adaptive smart auto-routing when model is 'auto'.
        """
        if model_name == "auto" and messages:
            complexity = self.classify_complexity(messages)
            if complexity == "simple":
                # Tier 1: Fast, highly cost-effective model
                return "groq", "llama-3.1-8b-instant", ["openai/gpt-4o-mini", "gemini/gemini-1.5-flash"]
            else:
                # Tier 2: Flagship / Deep reasoning model
                return "groq", "llama-3.3-70b-versatile", ["openai/gpt-4o", "gemini/gemini-1.5-pro"]

        # Format might be 'provider/model' or just 'model'
        if "/" in model_name:
            p, m = model_name.split("/", 1)
            if p in self.providers and m in self.providers[p].get("models", {}):
                fallbacks = self.providers[p]["models"][m].get("fallbacks", [])
                return p, m, fallbacks

        for p, p_data in self.providers.items():
            models = p_data.get("models", {})
            if model_name in models:
                return p, model_name, models[model_name].get("fallbacks", [])

        # Default fallback if unknown
        for p, p_data in self.providers.items():
            for m, m_data in p_data.get("models", {}).items():
                return p, m, m_data.get("fallbacks", [])

        return "openai", "gpt-4o-mini", []

    def get_next_key(self, provider_name: str) -> str:
        keys = self.providers.get(provider_name, {}).get("api_keys", [])
        clean_keys = [k for k in keys if k and not k.startswith("${")]
        if not clean_keys:
            return ""
        idx = self.key_indices.get(provider_name, 0)
        chosen_key = clean_keys[idx % len(clean_keys)]
        self.key_indices[provider_name] = (idx + 1) % len(clean_keys)
        return chosen_key

    def get_model_cost(self, provider: str, model: str, in_tok: int, out_tok: int) -> float:
        m_cfg = self.providers.get(provider, {}).get("models", {}).get(model, {})
        in_rate = m_cfg.get("input_cost_per_m", 0.0) / 1_000_000.0
        out_rate = m_cfg.get("output_cost_per_m", 0.0) / 1_000_000.0
        return round((in_tok * in_rate) + (out_tok * out_rate), 6)

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
            if finish_reason == "stop":
                finish_reason = "stop"

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

    async def execute_request(
        self,
        request_data: Dict[str, Any],
        stream: bool = False,
    ) -> Tuple[Union[Dict[str, Any], AsyncGenerator[str, None]], str, str]:
        """Execute request with automatic key rotation and failover on 429/503."""
        requested_model = request_data.get("model", "gpt-4o-mini")
        provider, model, fallbacks = self.resolve_model(requested_model, request_data.get("messages"))

        candidates_queue = [(provider, model)] + [self.resolve_model(fb)[:2] for fb in fallbacks]
        client = await self.get_client()

        last_error = None
        for p, m in candidates_queue:
            p_data = self.providers.get(p, {})
            p_type = p_data.get("type", "openai")
            base_url = p_data.get("base_url", "").rstrip("/")
            num_keys = max(1, len(p_data.get("api_keys", [])))

            # Attempt all keys for this provider if rate-limited
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
                elif p_type == "gemini":
                    action = "streamGenerateContent?alt=sse&" if stream else "generateContent?"
                    url = f"{base_url}/models/{m}:{action}key={api_key}"
                    payload = self._build_gemini_payload(request_data)

                try:
                    if stream:
                        # Return an SSE generator
                        gen = self._stream_response(client, p_type, url, headers, payload, m)
                        # Probe the first chunk or stream directly
                        return gen, p, m
                    else:
                        resp = await client.post(url, headers=headers, json=payload, timeout=60.0)
                        if resp.status_code in (429, 503):
                            logger.warning(
                                f"Provider {p} returned HTTP {resp.status_code}. Rotating/failing over."
                            )
                            last_error = f"HTTP {resp.status_code}: {resp.text}"
                            continue

                        resp.raise_for_status()
                        res_json = resp.json()
                        if p_type == "gemini":
                            res_json = self._convert_gemini_to_openai(res_json, m)
                        return res_json, p, m

                except httpx.HTTPStatusError as e:
                    if e.response.status_code in (429, 503):
                        logger.warning(f"Provider {p} returned {e.response.status_code}. Retrying...")
                        last_error = str(e)
                        continue
                    raise e
                except (httpx.ConnectError, httpx.TimeoutException) as e:
                    logger.warning(f"Connection error contacting {p}: {e}")
                    last_error = str(e)
                    break

        raise RuntimeError(f"All providers and fallbacks exhausted. Last error: {last_error}")

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
        if p_type != "gemini":
            payload["stream"] = True

        async with client.stream("POST", url, headers=headers, json=payload, timeout=60.0) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if p_type == "gemini":
                    # Convert gemini chunk to openai chunk
                    if line.startswith("data: "):
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            yield "data: [DONE]\n\n"
                            continue
                        try:
                            g_data = json.loads(data_str)
                            parts = (
                                g_data.get("candidates", [{}])[0]
                                .get("content", {})
                                .get("parts", [{}])
                            )
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
                else:
                    yield f"{line}\n\n"
