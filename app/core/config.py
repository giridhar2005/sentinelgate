from __future__ import annotations

import os
import re
from typing import Any, Dict, List

import yaml
from pydantic import BaseModel, Field


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000


class CacheConfig(BaseModel):
    enabled: bool = True
    similarity_threshold: float = 0.92
    ttl_seconds: int = 3600
    db_path: str = "sentinelgate.db"


class GuardrailsConfig(BaseModel):
    pii_masking: bool = True
    block_secrets: bool = True
    prompt_injection_block: bool = True
    max_repair_attempts: int = 1
    prompt_optimization: bool = True
    context_compaction: bool = True


class BudgetConfig(BaseModel):
    enabled: bool = True
    daily_limit_usd: float = 10.0


class EvalsConfig(BaseModel):
    enabled: bool = True
    sample_rate: float = 0.10
    cheaper_fallback_model: str = "groq/llama-3.1-8b-instant"
    judge_model: str = "gemini/gemini-1.5-flash"


class ProviderModelConfig(BaseModel):
    input_cost_per_m: float = 0.0
    output_cost_per_m: float = 0.0
    fallbacks: List[str] = Field(default_factory=list)


class ProviderConfig(BaseModel):
    type: str = "openai"  # openai, anthropic, groq, gemini, ollama, openai_compatible
    base_url: str = ""
    api_keys: List[str] = Field(default_factory=list)
    models: Dict[str, ProviderModelConfig] = Field(default_factory=dict)


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    evals: EvalsConfig = Field(default_factory=EvalsConfig)
    providers: Dict[str, ProviderConfig] = Field(default_factory=dict)


def expand_env_vars(val: Any) -> Any:
    if isinstance(val, str):
        pattern = re.compile(r"\$\{([^}]+)\}")
        matches = pattern.findall(val)
        for m in matches:
            val = val.replace(f"${{{m}}}", os.getenv(m, ""))
        return val
    elif isinstance(val, list):
        return [expand_env_vars(x) for x in val]
    elif isinstance(val, dict):
        return {k: expand_env_vars(v) for k, v in val.items()}
    return val


def load_config(config_path: str = "config.yaml") -> AppConfig:
    resolved_path = os.getenv("SENTINELGATE_CONFIG", config_path)
    if not os.path.exists(resolved_path):
        return AppConfig()
    try:
        with open(resolved_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        expanded = expand_env_vars(raw)
        return AppConfig.model_validate(expanded)
    except Exception:
        return AppConfig()
