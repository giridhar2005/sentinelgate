from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple


def score_complexity(messages: List[Dict[str, Any]]) -> Tuple[int, str, Dict[str, Any]]:
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


def classify_complexity(messages: List[Dict[str, Any]]) -> str:
    _, classification, _ = score_complexity(messages)
    return classification


def resolve_model(
    model_name: str,
    providers: Dict[str, Any],
    is_provider_ready: Callable[[str], bool],
    messages: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, str, List[str], Optional[Dict[str, Any]]]:
    """Resolve an incoming model request to (provider, model_name, fallbacks, routing_meta)."""
    meta_info: Optional[Dict[str, Any]] = None
    if model_name == "auto" and messages:
        score, classification, meta = score_complexity(messages)
        meta_info = {
            "model": "auto",
            "score": score,
            "classification": classification,
            "meta": meta,
        }
        if classification == "simple":
            # Route to cheapest/fastest available model
            if is_provider_ready("ollama"):
                return (
                    "ollama",
                    "llama3",
                    [
                        "groq/llama-3.1-8b-instant",
                        "gemini/gemini-1.5-flash",
                        "openai/gpt-4o-mini",
                    ],
                    meta_info,
                )
            if is_provider_ready("groq"):
                return (
                    "groq",
                    "llama-3.1-8b-instant",
                    ["gemini/gemini-1.5-flash", "openai/gpt-4o-mini"],
                    meta_info,
                )
            if is_provider_ready("gemini"):
                return (
                    "gemini",
                    "gemini-1.5-flash",
                    ["openai/gpt-4o-mini", "groq/llama-3.1-8b-instant"],
                    meta_info,
                )
            if is_provider_ready("openai"):
                return (
                    "openai",
                    "gpt-4o-mini",
                    ["groq/llama-3.1-8b-instant", "gemini/gemini-1.5-flash"],
                    meta_info,
                )
            return (
                "groq",
                "llama-3.1-8b-instant",
                ["openai/gpt-4o-mini", "gemini/gemini-1.5-flash"],
                meta_info,
            )
        else:
            # Route to frontier model for complex reasoning/coding
            if is_provider_ready("anthropic"):
                return (
                    "anthropic",
                    "claude-3-5-sonnet-20241022",
                    ["openai/gpt-4o", "gemini/gemini-1.5-pro"],
                    meta_info,
                )
            if is_provider_ready("openai"):
                return (
                    "openai",
                    "gpt-4o",
                    ["groq/llama-3.3-70b-versatile", "gemini/gemini-1.5-pro"],
                    meta_info,
                )
            if is_provider_ready("groq"):
                return (
                    "groq",
                    "llama-3.3-70b-versatile",
                    ["openai/gpt-4o", "gemini/gemini-1.5-pro"],
                    meta_info,
                )
            if is_provider_ready("gemini"):
                return (
                    "gemini",
                    "gemini-1.5-pro",
                    ["openai/gpt-4o", "anthropic/claude-3-5-sonnet-20241022"],
                    meta_info,
                )
            return (
                "openai",
                "gpt-4o",
                ["groq/llama-3.3-70b-versatile", "anthropic/claude-3-5-sonnet-20241022"],
                meta_info,
            )

    if "/" in model_name:
        p, m = model_name.split("/", 1)
        p_cfg = providers.get(p)
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
            return p, m, fallbacks, meta_info

    for p, p_cfg in providers.items():
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
            return p, model_name, fallbacks, meta_info

    # Default fallback
    for p, p_cfg in providers.items():
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
            return p, m, fallbacks, meta_info

    return "openai", "gpt-4o-mini", [], meta_info


def get_all_models(providers: Dict[str, Any]) -> List[Dict[str, Any]]:
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
    for p_name, p_data in providers.items():
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
