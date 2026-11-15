from __future__ import annotations

import asyncio
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

        # Ollama local models
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

        # LM Studio local server
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

        # OpenRouter cloud
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
                models.append(
                    {
                        "id": m_name,
                        "object": "model",
                        "created": now,
                        "owned_by": p_name,
                        "permission": [],
                        "root": m_name,
                        "parent": None,
                    }
                )
        return models

    def score_complexity(self, messages: List[Dict[str, Any]]) -> Tuple[int, str, Dict[str, Any]]:
        """Fast sub-millisecond deterministic prompt complexity evaluator."""
        full_text = " ".join(
            [str(m.get("content", "")) for m in messages if isinstance(m.get("content"), str)]
        )
        lower_text = full_text.lower()
        total_chars = len(full_text)
        num_turns = len(messages)

        score = 0
        factors = []

        # 1. Length & context depth
        if total_chars > 1500:
            score += 35
            factors.append(f"large_context ({total_chars} chars)")
        elif total_chars > 450:
            score += 15
            factors.append(f"moderate_context ({total_chars} chars)")

        if num_turns >= 6:
            score += 20
            factors.append(f"multi_turn ({num_turns} messages)")

        # 2. Code blocks, syntax & diffs
        code_signals = [
            "```",
            "diff --git",
            "def ",
            "class ",
            "function ",
            "import ",
            "select ",
            "from ",
            "curl ",
            "dockerfile",
        ]
        code_hits = sum(1 for s in code_signals if s in lower_text)
        if "```" in full_text or "diff --git" in lower_text:
            score += 30
            factors.append("code_blocks")
        elif code_hits >= 2:
            score += 15
            factors.append("code_syntax")

        # 3. High-complexity reasoning keywords
        complex_keywords = [
            "architect",
            "architecture",
            "derive",
            "mathematical",
            "proof",
            "refactor",
            "optimize",
            "tradeoff",
            "trade-off",
            "benchmark",
            "concurrency",
            "distributed",
            "system design",
            "memory leak",
            "race condition",
            "deadlock",
            "algorithm",
            "debug",
            "traceback",
            "stack trace",
            "why does",
            "explain step by step",
            "step-by-step",
            "security vulnerability",
            "audit",
        ]
        matched_kws = [kw for kw in complex_keywords if kw in lower_text]
        if len(matched_kws) >= 3:
            score += 50
            factors.append(f"reasoning_keywords ({', '.join(matched_kws[:3])})")
        elif len(matched_kws) >= 2:
            score += 35
            factors.append(f"reasoning_keywords ({', '.join(matched_kws[:2])})")
        elif len(matched_kws) == 1:
            score += 20
            factors.append(f"reasoning_keyword ({matched_kws[0]})")

        # 4. Routine / simple query pattern downweight
        simple_words = {"hello", "hi", "hey", "ping", "test", "capitalize", "convert"}
        simple_phrases = ["summarize", "format as json", "fix spelling", "what is"]
        words = set(re.findall(r"\b\w+\b", lower_text))
        is_routine = any(w in words for w in simple_words) or any(
            p in lower_text for p in simple_phrases
        )
        if is_routine and total_chars < 300 and not code_hits and len(matched_kws) == 0:
            score = max(0, score - 25)
            factors.append("routine_query")

        final_score = min(100, max(0, score))
        classification = "complex" if final_score >= 45 else "simple"
        meta = {
            "score": final_score,
            "classification": classification,
            "factors": factors,
            "length_chars": total_chars,
            "num_messages": num_turns,
        }
        return final_score, classification, meta

    def classify_complexity(self, messages: List[Dict[str, Any]]) -> str:
        _, classification, _ = self.score_complexity(messages)
        return classification

    def resolve_model(
        self,
        model_name: str,
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[str, str, List[str]]:
        if model_name == "auto" and messages:
            score, classification, meta = self.score_complexity(messages)
            self.last_routing_metadata = {
                "model": "auto",
                "score": score,
                "classification": classification,
                "meta": meta,
            }
            if classification == "simple":
                # Route to cheapest/fastest available model
                if self._is_provider_ready("ollama"):
                    return (
                        "ollama",
                        "llama3",
                        [
                            "groq/llama-3.1-8b-instant",
                            "gemini/gemini-1.5-flash",
                            "openai/gpt-4o-mini",
                        ],
                    )
                if self._is_provider_ready("groq"):
                    return (
                        "groq",
                        "llama-3.1-8b-instant",
                        ["gemini/gemini-1.5-flash", "openai/gpt-4o-mini"],
                    )
                if self._is_provider_ready("gemini"):
                    return (
                        "gemini",
                        "gemini-1.5-flash",
                        ["openai/gpt-4o-mini", "groq/llama-3.1-8b-instant"],
                    )
                if self._is_provider_ready("openai"):
                    return (
                        "openai",
                        "gpt-4o-mini",
                        ["groq/llama-3.1-8b-instant", "gemini/gemini-1.5-flash"],
                    )
                return (
                    "groq",
                    "llama-3.1-8b-instant",
                    ["openai/gpt-4o-mini", "gemini/gemini-1.5-flash"],
                )
            else:
                # Route to frontier model for complex reasoning/coding
                if self._is_provider_ready("anthropic"):
                    return (
                        "anthropic",
                        "claude-3-5-sonnet-20241022",
                        ["openai/gpt-4o", "gemini/gemini-1.5-pro"],
                    )
                if self._is_provider_ready("openai"):
                    return (
                        "openai",
                        "gpt-4o",
                        ["groq/llama-3.3-70b-versatile", "gemini/gemini-1.5-pro"],
                    )
                if self._is_provider_ready("groq"):
                    return (
                        "groq",
                        "llama-3.3-70b-versatile",
                        ["openai/gpt-4o", "gemini/gemini-1.5-pro"],
                    )
                if self._is_provider_ready("gemini"):
                    return (
                        "gemini",
                        "gemini-1.5-pro",
                        ["openai/gpt-4o", "anthropic/claude-3-5-sonnet-20241022"],
                    )
                return (
                    "openai",
                    "gpt-4o",
                    ["groq/llama-3.3-70b-versatile", "anthropic/claude-3-5-sonnet-20241022"],
                )

        if "/" in model_name:
            p, m = model_name.split("/", 1)
            p_cfg = self.providers.get(p)
            models = (
                p_cfg.models
                if p_cfg and hasattr(p_cfg, "models")
                else (p_cfg.get("models", {}) if isinstance(p_cfg, dict) else {})
            )
            if m in models:
                m_cfg = models[m]
                fallbacks = (
                    getattr(m_cfg, "fallbacks", [])
                    if hasattr(m_cfg, "fallbacks")
                    else m_cfg.get("fallbacks", [])
                )
                return p, m, fallbacks

        for p, p_cfg in self.providers.items():
            models = (
                p_cfg.models
                if hasattr(p_cfg, "models")
                else (p_cfg.get("models", {}) if isinstance(p_cfg, dict) else {})
            )
            if model_name in models:
                m_cfg = models[model_name]
                fallbacks = (
                    getattr(m_cfg, "fallbacks", [])
                    if hasattr(m_cfg, "fallbacks")
                    else m_cfg.get("fallbacks", [])
                )
                return p, model_name, fallbacks

        # Default fallback
        for p, p_cfg in self.providers.items():
            models = (
                p_cfg.models
                if hasattr(p_cfg, "models")
                else (p_cfg.get("models", {}) if isinstance(p_cfg, dict) else {})
            )
            for m, m_cfg in models.items():
                fallbacks = (
                    getattr(m_cfg, "fallbacks", [])
                    if hasattr(m_cfg, "fallbacks")
                    else m_cfg.get("fallbacks", [])
                )
                return p, m, fallbacks

        return "openai", "gpt-4o-mini", []

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
        """Return (input_cost_per_m, output_cost_per_m) for a given provider and model."""
        p_cfg = self.providers.get(provider)
        models = (
            getattr(p_cfg, "models", {})
            if hasattr(p_cfg, "models")
            else (p_cfg.get("models", {}) if isinstance(p_cfg, dict) else {})
        )
        m_cfg = models.get(model, {})
        in_rate = (
            getattr(m_cfg, "input_cost_per_m", 0.0)
            if hasattr(m_cfg, "input_cost_per_m")
            else m_cfg.get("input_cost_per_m", 0.0)
        )
        out_rate = (
            getattr(m_cfg, "output_cost_per_m", 0.0)
            if hasattr(m_cfg, "output_cost_per_m")
            else m_cfg.get("output_cost_per_m", 0.0)
        )
        if in_rate == 0.0 and out_rate == 0.0:
            defaults = {
                "gpt-4o": (5.0, 15.0),
                "gpt-4o-mini": (0.15, 0.60),
                "claude-3-5-sonnet-20241022": (3.0, 15.0),
                "claude-3-5-haiku-20241022": (0.80, 4.0),
                "llama-3.3-70b-versatile": (0.59, 0.79),
                "llama-3.1-8b-instant": (0.05, 0.08),
                "gemini-1.5-flash": (0.075, 0.30),
                "gemini-1.5-pro": (3.50, 10.50),
            }
            return defaults.get(model, (0.15, 0.60))
        return float(in_rate), float(out_rate)

    def get_model_cost(self, provider: str, model: str, in_tok: int, out_tok: int) -> float:
        in_rate, out_rate = self.get_model_pricing(provider, model)
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

    def _generate_sandbox_response(
        self,
        model: str,
        provider: str,
        messages: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Generate a deterministic, realistic LLM completion for zero-key sandbox evaluation."""
        user_content = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_content = str(m.get("content", ""))
                break
        if not user_content:
            user_content = "Default prompt"

        lower = user_content.lower()
        prompt_tokens = max(1, sum(len(str(m.get("content", ""))) // 4 for m in messages))

        if (
            "rm -rf" in lower
            or "agent tool attack" in lower
            or "drop table" in lower
            or "bash(" in lower
        ):
            return {
                "id": f"chatcmpl-sandbox-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": f"call_sandbox_{uuid.uuid4().hex[:8]}",
                                    "type": "function",
                                    "function": {
                                        "name": "bash",
                                        "arguments": json.dumps(
                                            {"command": "rm -rf / --no-preserve-root"}
                                        ),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": 15,
                    "total_tokens": prompt_tokens + 15,
                },
            }

        if "raft" in lower:
            text = (
                "Raft is a leader-based consensus algorithm designed for distributed state machine replication. "
                "It manages log replication, leader elections, and joint-consensus cluster membership changes "
                "with provable formal safety invariants and predictable state transitions."
            )
        elif "quick sort" in lower or "quicksort" in lower:
            text = (
                "QuickSort is an in-place, divide-and-conquer sorting algorithm with average-case time complexity "
                "of O(N log N). It selects a pivot, partitions elements into sub-arrays according to the pivot, "
                "and recursively sorts each sub-array with O(log N) stack overhead."
            )
        elif "binary search" in lower:
            text = (
                "Binary search operates on sorted arrays by repeatedly halving the search interval. "
                "Comparing the target with the median index element achieves logarithmic O(log N) runtime "
                "with O(1) auxiliary space complexity."
            )
        elif "hello" in lower or "hi" in lower:
            text = (
                "Hello! SentinelGate is active in deterministic zero-key sandbox mode with low-latency local "
                "inference simulation, semantic caching, and full OpenAI REST compatibility."
            )
        else:
            snippet = user_content[:70].strip()
            text = (
                f"SentinelGate Sandbox [{provider}/{model}]: Verified response for prompt '{snippet}'. "
                "Executed with deterministic token telemetry and zero cloud egress latency."
            )

        prompt_tokens = max(1, sum(len(str(m.get("content", ""))) // 4 for m in messages))
        completion_tokens = max(1, len(text) // 4)

        return {
            "id": f"chatcmpl-sandbox-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    async def _stream_sandbox_response(
        self,
        model: str,
        messages: List[Dict[str, Any]],
    ) -> AsyncGenerator[str, None]:
        """Stream a deterministic response token by token via SSE."""
        res = self._generate_sandbox_response(model, "sandbox", messages)
        text = res["choices"][0]["message"]["content"]
        req_id = f"chatcmpl-sandbox-{uuid.uuid4().hex[:12]}"
        words = text.split(" ")
        for i, word in enumerate(words):
            token = word + (" " if i < len(words) - 1 else "")
            chunk = {
                "id": req_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {"content": token}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
            await asyncio.sleep(0.015)

        done_chunk = {
            "id": req_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(done_chunk)}\n\n"
        yield "data: [DONE]\n\n"

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
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    def _build_anthropic_payload(
        self, req: Dict[str, Any], model: str, stream: bool = False
    ) -> Dict[str, Any]:
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
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": anth_data.get("stop_reason", "stop"),
                }
            ],
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
        provider, model, fallbacks = self.resolve_model(
            requested_model, request_data.get("messages")
        )

        candidates_queue = [(provider, model)] + [self.resolve_model(fb)[:2] for fb in fallbacks]
        run_queue = self.circuit_breaker.filter_available_candidates(candidates_queue)
        client = await self.get_client()

        last_error = None
        for p, m in run_queue:
            if self.chaos_enabled and p == self.chaos_target_provider:
                logger.warning(
                    f"[CHAOS FAULT INJECTION] Injected HTTP {self.chaos_status_code} failure on provider '{p}'"
                )
                self.record_provider_failure(p, self.chaos_status_code)
                last_error = (
                    f"Chaos fault injection: simulated HTTP {self.chaos_status_code} on {p}"
                )
                continue

            if self.is_sandbox_mode():
                # Zero-key sandbox simulation
                await asyncio.sleep(0.015)
                if stream:
                    gen = self._stream_sandbox_response(m, request_data.get("messages", []))
                    self.record_provider_success(p)
                    return gen, p, m
                else:
                    res_json = self._generate_sandbox_response(
                        m, p, request_data.get("messages", [])
                    )
                    self.record_provider_success(p)
                    return res_json, p, m

            p_cfg = self.providers.get(p)
            p_type = (
                getattr(p_cfg, "type", "openai")
                if hasattr(p_cfg, "type")
                else (p_cfg.get("type", "openai") if isinstance(p_cfg, dict) else "openai")
            )
            base_url = (
                getattr(p_cfg, "base_url", "")
                if hasattr(p_cfg, "base_url")
                else (p_cfg.get("base_url", "") if isinstance(p_cfg, dict) else "")
            ).rstrip("/")
            keys = (
                getattr(p_cfg, "api_keys", [])
                if hasattr(p_cfg, "api_keys")
                else (p_cfg.get("api_keys", []) if isinstance(p_cfg, dict) else [])
            )
            num_keys = max(1, len(keys))

            for _ in range(num_keys):
                api_key = self.get_next_key(p)
                headers = {"Content-Type": "application/json"}
                if "_headers" in request_data and isinstance(request_data["_headers"], dict):
                    headers.update(request_data["_headers"])
                url = ""
                payload = dict(request_data)
                payload.pop("_headers", None)
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
                            logger.warning(
                                f"Provider {p} returned {resp.status_code}. Failing over/rotating."
                            )
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
    ) -> Tuple[Union[Dict[str, Any], AsyncGenerator[str, None]], str, str, Dict[str, Any]]:
        """Speculative Latency Hedging (Google Tail at Scale p99 Slasher).

        Races the primary upstream request against a concurrent secondary hedge if the primary
        exceeds hedge_delay_sec. Slashes p99 tail latencies caused by upstream queuing delays.
        """
        primary_task = asyncio.create_task(self.execute_request(request_data, stream=False))
        done, _ = await asyncio.wait([primary_task], timeout=hedge_delay_sec)
        if primary_task in done:
            self.hedged_primary_won_count += 1
            res, p, m = await primary_task
            return (
                res,
                p,
                m,
                {"hedged_triggered": False, "winner": "primary", "delay_sec": hedge_delay_sec},
            )

        # Primary delayed beyond SLA threshold: trigger speculative hedge
        self.hedged_triggered_count += 1
        requested_model = request_data.get("model", "gpt-4o-mini")
        primary_provider, _, fallbacks = self.resolve_model(
            requested_model, request_data.get("messages")
        )

        hedge_model = requested_model
        if fallbacks:
            hedge_model = fallbacks[0]
        elif primary_provider != "groq" and "groq" in self.providers:
            hedge_model = "groq/llama-3.1-8b-instant"
        elif primary_provider != "gemini" and "gemini" in self.providers:
            hedge_model = "gemini/gemini-1.5-flash"
        elif primary_provider != "openai" and "openai" in self.providers:
            hedge_model = "openai/gpt-4o-mini"

        hedged_payload = dict(request_data)
        hedged_payload["model"] = hedge_model
        hedge_task = asyncio.create_task(self.execute_request(hedged_payload, stream=False))

        # Race with asyncio.as_completed so if one task raises, the other can still succeed
        first_success = None
        last_exc = None

        tasks = [primary_task, hedge_task]
        for coro in asyncio.as_completed(tasks):
            try:
                res, p, m = await coro
                first_success = (res, p, m)
                break
            except Exception as e:
                logger.warning(f"Task in hedged race failed: {e}")
                last_exc = e

        # Cancel any pending uncompleted tasks immediately
        for t in tasks:
            if not t.done():
                t.cancel()

        if first_success:
            res, p, m = first_success
            is_hedge = (
                (p != primary_provider and not p.endswith("(cached)"))
                or "groq" in p
                or "llama" in m
                or p == "gemini"
            )
            if is_hedge:
                self.hedged_secondary_won_count += 1
                return (
                    res,
                    f"{p}(hedged)",
                    m,
                    {
                        "hedged_triggered": True,
                        "winner": "hedge",
                        "hedge_provider": p,
                        "delay_sec": hedge_delay_sec,
                    },
                )
            else:
                self.hedged_primary_won_count += 1
                return (
                    res,
                    p,
                    m,
                    {
                        "hedged_triggered": True,
                        "winner": "primary",
                        "delay_sec": hedge_delay_sec,
                    },
                )

        raise RuntimeError(f"Hedged execution failed across all runners. Last error: {last_exc}")

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
                                "choices": [
                                    {"index": 0, "delta": {"content": text}, "finish_reason": None}
                                ],
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
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {"content": delta_text},
                                            "finish_reason": None,
                                        }
                                    ],
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
                embeddings_list.append(
                    {
                        "object": "embedding",
                        "index": i,
                        "embedding": [round(float(x), 6) for x in vec],
                    }
                )
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
