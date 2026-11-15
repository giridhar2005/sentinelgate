from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx
import yaml

from sentinelgate.core.circuit import ProviderCircuitBreaker
from sentinelgate.core.config import AppConfig, expand_env_vars
from sentinelgate.services.router.failover import execute_failover_loop, stream_response_lines
from sentinelgate.services.router.hedging import execute_hedged_request as _execute_hedged
from sentinelgate.services.router.providers.anthropic import (
    build_anthropic_payload,
    convert_anthropic_to_openai,
)
from sentinelgate.services.router.providers.base import calculate_cost, get_model_pricing
from sentinelgate.services.router.providers.gemini import (
    build_gemini_payload,
    convert_gemini_to_openai,
)
from sentinelgate.services.router.providers.sandbox import (
    generate_sandbox_response,
    stream_sandbox_response,
)
from sentinelgate.services.router.routing import (
    classify_complexity,
    get_all_models,
    resolve_model,
    score_complexity,
)

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
        self.chaos_enabled: bool = False
        self.chaos_target_provider: str = "openai"
        self.chaos_status_code: int = 429
        self.demo_mode: Optional[bool] = None
        self.discovery_info: Dict[str, Any] = {}
        self.last_routing_metadata: Dict[str, Any] = {}
        self.hedged_triggered_count: int = 0
        self.hedged_primary_won_count: int = 0
        self.hedged_secondary_won_count: int = 0

    def apply_discovery(self, discovery_info: Dict[str, Any]):
        """Incorporate discovered local runtimes and environment keys into routing inventory."""
        self.discovery_info = discovery_info
        local = discovery_info.get("local", {})

        ollama = local.get("ollama", {})
        if ollama.get("active") and "ollama" not in self.providers:
            self.providers["ollama"] = {
                "type": "ollama",
                "base_url": ollama.get("url", "http://127.0.0.1:11434") + "/v1",
                "api_keys": ["ollama-local"],
                "models": {
                    "llama3": {
                        "input_cost_per_m": 0.0,
                        "output_cost_per_m": 0.0,
                        "fallbacks": ["groq/llama-3.1-8b-instant"],
                    },
                    "mistral": {
                        "input_cost_per_m": 0.0,
                        "output_cost_per_m": 0.0,
                        "fallbacks": ["groq/llama-3.1-8b-instant"],
                    },
                },
            }

        lmstudio = local.get("lmstudio", {})
        if lmstudio.get("active") and "lmstudio" not in self.providers:
            self.providers["lmstudio"] = {
                "type": "openai_compatible",
                "base_url": lmstudio.get("url", "http://127.0.0.1:1234") + "/v1",
                "api_keys": ["lmstudio-local"],
                "models": {
                    "local-model": {
                        "input_cost_per_m": 0.0,
                        "output_cost_per_m": 0.0,
                        "fallbacks": ["groq/llama-3.1-8b-instant"],
                    },
                },
            }

        openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        if openrouter_key and "openrouter" not in self.providers:
            self.providers["openrouter"] = {
                "type": "openai_compatible",
                "base_url": "https://openrouter.ai/api/v1",
                "api_keys": [openrouter_key],
                "models": {
                    "auto": {"input_cost_per_m": 0.50, "output_cost_per_m": 1.50, "fallbacks": []},
                },
            }

    def _is_provider_ready(self, provider: str) -> bool:
        if self.is_sandbox_mode():
            return True
        if provider in ("ollama", "lmstudio"):
            local_info = self.discovery_info.get("local", {}).get(provider, {})
            return bool(local_info.get("active"))
        p_cfg = self.providers.get(provider)
        if not p_cfg:
            return False
        keys = (
            getattr(p_cfg, "api_keys", [])
            if hasattr(p_cfg, "api_keys")
            else (p_cfg.get("api_keys", []) if isinstance(p_cfg, dict) else [])
        )
        clean = [k for k in keys if k and not str(k).startswith("${")]
        return len(clean) > 0

    def has_live_keys(self) -> bool:
        """Check if any provider has non-placeholder cloud API keys configured."""
        for p, p_cfg in self.providers.items():
            if p == "ollama":
                continue
            keys = (
                getattr(p_cfg, "api_keys", [])
                if hasattr(p_cfg, "api_keys")
                else (p_cfg.get("api_keys", []) if isinstance(p_cfg, dict) else [])
            )
            clean = [k for k in keys if k and not str(k).startswith("${") and str(k).strip() != ""]
            if clean:
                return True
        return False

    def is_sandbox_mode(self) -> bool:
        """Determines if the gateway should run in zero-key deterministic sandbox mode."""
        if getattr(self, "demo_mode", None) is not None:
            return bool(self.demo_mode)
        demo_env = os.getenv("DEMO_MODE")
        if demo_env is not None:
            return demo_env.strip().lower() in ("true", "1", "yes")
        return not self.has_live_keys()

    def set_chaos(self, enabled: bool, target_provider: str = "openai", status_code: int = 429):
        """Toggle chaos fault injection on a target provider."""
        self.chaos_enabled = enabled
        self.chaos_target_provider = target_provider
        self.chaos_status_code = status_code
        if not enabled:
            self.circuit_breaker.record_success(target_provider)
        logger.info(
            f"Chaos fault injection: enabled={enabled}, target={target_provider}, status_code={status_code}"
        )

    def _load_config(self) -> AppConfig:
        path = os.getenv("SENTINELGATE_CONFIG", self.config_path)
        if not os.path.exists(path):
            if path == "config.yaml" and os.path.exists("config.example.yaml"):
                path = "config.example.yaml"
            else:
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
        return get_all_models(self.providers)

    def score_complexity(self, messages: List[Dict[str, Any]]) -> Tuple[int, str, Dict[str, Any]]:
        return score_complexity(messages)

    def classify_complexity(self, messages: List[Dict[str, Any]]) -> str:
        return classify_complexity(messages)

    def resolve_model(
        self,
        model_name: str,
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[str, str, List[str]]:
        p, m, fallbacks, meta = resolve_model(
            model_name, self.providers, self._is_provider_ready, messages
        )
        if meta:
            self.last_routing_metadata = meta
        return p, m, fallbacks

    def get_next_key(self, provider_name: str) -> str:
        p_cfg = self.providers.get(provider_name)
        keys = (
            getattr(p_cfg, "api_keys", [])
            if hasattr(p_cfg, "api_keys")
            else (p_cfg.get("api_keys", []) if isinstance(p_cfg, dict) else [])
        )
        clean_keys = [k for k in keys if k and not str(k).startswith("${")]
        if not clean_keys:
            return ""
        idx = self.key_indices.get(provider_name, 0)
        chosen_key = clean_keys[idx % len(clean_keys)]
        self.key_indices[provider_name] = (idx + 1) % len(clean_keys)
        return chosen_key

    def get_model_pricing(self, provider: str, model: str) -> Tuple[float, float]:
        return get_model_pricing(provider, model, self.providers)

    def get_model_cost(self, provider: str, model: str, in_tok: int, out_tok: int) -> float:
        return calculate_cost(provider, model, in_tok, out_tok, self.providers)

    def is_provider_available(self, provider: str) -> bool:
        return self.circuit_breaker.is_available(provider)

    def record_provider_failure(self, provider: str, status_code: int = 500):
        self.circuit_breaker.record_failure(provider, status_code)

    def record_provider_success(self, provider: str):
        self.circuit_breaker.record_success(provider)

    def get_provider_statuses(self) -> Dict[str, Any]:
        return self.circuit_breaker.get_statuses(self.providers)

    def _generate_sandbox_response(
        self,
        model: str,
        provider: str,
        messages: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        return generate_sandbox_response(model, provider, messages)

    async def _stream_sandbox_response(
        self,
        model: str,
        messages: List[Dict[str, Any]],
    ) -> AsyncGenerator[str, None]:
        async for chunk in stream_sandbox_response(model, messages):
            yield chunk

    def _build_gemini_payload(self, req: Dict[str, Any]) -> Dict[str, Any]:
        return build_gemini_payload(req)

    def _convert_gemini_to_openai(self, gemini_data: Dict[str, Any], model: str) -> Dict[str, Any]:
        return convert_gemini_to_openai(gemini_data, model)

    def _build_anthropic_payload(
        self, req: Dict[str, Any], model: str, stream: bool = False
    ) -> Dict[str, Any]:
        return build_anthropic_payload(req, model, stream=stream)

    def _convert_anthropic_to_openai(self, anth_data: Dict[str, Any], model: str) -> Dict[str, Any]:
        return convert_anthropic_to_openai(anth_data, model)

    async def execute_request(
        self,
        request_data: Dict[str, Any],
        stream: bool = False,
    ) -> Tuple[Union[Dict[str, Any], AsyncGenerator[str, None]], str, str]:
        requested_model = request_data.get("model", "gpt-4o-mini")
        provider, model, fallbacks = self.resolve_model(
            requested_model, request_data.get("messages")
        )

        candidates_queue = [(provider, model)] + [self.resolve_model(fb)[:2] for fb in fallbacks]
        run_queue = self.circuit_breaker.filter_available_candidates(candidates_queue)
        client = await self.get_client()

        chaos_cfg = {
            "enabled": self.chaos_enabled,
            "target_provider": self.chaos_target_provider,
            "status_code": self.chaos_status_code,
        }

        return await execute_failover_loop(
            request_data=request_data,
            stream=stream,
            candidates=run_queue,
            providers=self.providers,
            client=client,
            is_sandbox=self.is_sandbox_mode(),
            chaos_config=chaos_cfg,
            get_key_fn=self.get_next_key,
            record_success_fn=self.record_provider_success,
            record_failure_fn=self.record_provider_failure,
        )

    async def execute_hedged_request(
        self,
        request_data: Dict[str, Any],
        hedge_delay_sec: float = 0.45,
    ) -> Tuple[Union[Dict[str, Any], AsyncGenerator[str, None]], str, str, Dict[str, Any]]:
        def _inc_primary():
            self.hedged_primary_won_count += 1

        def _inc_secondary():
            self.hedged_secondary_won_count += 1

        def _inc_triggered():
            self.hedged_triggered_count += 1

        return await _execute_hedged(
            request_data=request_data,
            execute_request_fn=self.execute_request,
            resolve_model_fn=self.resolve_model,
            available_providers=self.providers,
            record_winner_primary_fn=_inc_primary,
            record_winner_secondary_fn=_inc_secondary,
            record_hedged_triggered_fn=_inc_triggered,
            hedge_delay_sec=hedge_delay_sec,
        )

    async def _stream_response(
        self,
        client: httpx.AsyncClient,
        p_type: str,
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
        model: str,
    ) -> AsyncGenerator[str, None]:
        async for line in stream_response_lines(client, p_type, url, headers, payload, model):
            yield line

    async def execute_embeddings(self, model: str, inputs: Union[str, List[str]]) -> Dict[str, Any]:
        """Compute embeddings either locally via FastEmbed or by proxying to an embedding provider."""
        text_list = [inputs] if isinstance(inputs, str) else inputs
        embeddings_list = []

        try:
            from fastembed import TextEmbedding

            embedder = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
            raw_embeddings = list(embedder.embed(text_list))
            for i, vec in enumerate(raw_embeddings):
                embeddings_list.append(
                    {
                        "object": "embedding",
                        "index": i,
                        "embedding": [round(float(x), 6) for x in vec],
                    }
                )
        except Exception:
            import hashlib

            import numpy as np

            for i, txt in enumerate(text_list):
                vec = np.zeros(384, dtype=float)
                for w in txt.lower().split():
                    h = int(hashlib.md5(w.encode("utf-8")).hexdigest(), 16)
                    vec[h % 384] += 1.0
                norm = np.linalg.norm(vec)
                normalized = (vec / norm if norm > 0 else vec).tolist()
                embeddings_list.append(
                    {
                        "object": "embedding",
                        "index": i,
                        "embedding": [round(float(x), 6) for x in normalized],
                    }
                )

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


__all__ = ["ProviderRouter"]
