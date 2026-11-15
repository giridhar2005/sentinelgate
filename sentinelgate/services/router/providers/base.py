from __future__ import annotations

from typing import Any, Dict, Tuple


def get_model_pricing(
    provider: str,
    model: str,
    providers_config: Dict[str, Any],
) -> Tuple[float, float]:
    """Return (input_cost_per_m, output_cost_per_m) for a given provider and model."""
    p_cfg = providers_config.get(provider)
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


def calculate_cost(
    provider: str,
    model: str,
    in_tok: int,
    out_tok: int,
    providers_config: Dict[str, Any],
) -> float:
    in_rate, out_rate = get_model_pricing(provider, model, providers_config)
    return round(((in_tok * in_rate) + (out_tok * out_rate)) / 1_000_000.0, 6)
