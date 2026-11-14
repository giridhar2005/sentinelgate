from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("sentinelgate.discovery")


class ProviderDiscovery:
    """Zero-config auto-discovery for cloud API keys and local LLM runtime endpoints."""

    OLLAMA_URL = "http://127.0.0.1:11434"
    LMSTUDIO_URL = "http://127.0.0.1:1234"

    @classmethod
    async def probe_local_server(
        cls, url: str, path: str = "/api/tags", timeout: float = 0.30
    ) -> Optional[Dict[str, Any]]:
        """Fast non-blocking probe to verify if a local model server is reachable."""
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(f"{url.rstrip('/')}{path}")
                if resp.status_code == 200:
                    return resp.json()
        except Exception:
            pass
        return None

    @classmethod
    async def discover(cls) -> Dict[str, Any]:
        """Scan environment variables and probe local ports to assemble active provider inventory."""
        env_keys = {
            "openai": os.getenv("OPENAI_API_KEY", "").strip(),
            "anthropic": os.getenv("ANTHROPIC_API_KEY", "").strip(),
            "groq": os.getenv("GROQ_API_KEY", "").strip(),
            "gemini": os.getenv("GEMINI_API_KEY", "").strip(),
            "openrouter": os.getenv("OPENROUTER_API_KEY", "").strip(),
        }

        # Concurrently probe local servers with low latency timeout
        ollama_task = asyncio.create_task(cls.probe_local_server(cls.OLLAMA_URL, "/api/tags"))
        lmstudio_task = asyncio.create_task(cls.probe_local_server(cls.LMSTUDIO_URL, "/v1/models"))
        ollama_data, lmstudio_data = await asyncio.gather(ollama_task, lmstudio_task)

        cloud_status: Dict[str, Dict[str, Any]] = {}
        active_count = 0

        for p_name, key in env_keys.items():
            is_active = bool(key and not key.startswith("${"))
            cloud_status[p_name] = {
                "active": is_active,
                "preview": f"{key[:7]}...{key[-3:]}" if is_active and len(key) > 10 else ("configured" if is_active else None),
            }
            if is_active:
                active_count += 1

        local_status: Dict[str, Dict[str, Any]] = {}

        # Ollama local check
        ollama_online = ollama_data is not None and isinstance(ollama_data, dict)
        ollama_models = [m.get("name") for m in ollama_data.get("models", [])] if ollama_online else []
        local_status["ollama"] = {
            "active": ollama_online,
            "url": cls.OLLAMA_URL,
            "models": ollama_models,
        }
        if ollama_online:
            active_count += 1

        # LM Studio local check
        lmstudio_online = lmstudio_data is not None and isinstance(lmstudio_data, dict)
        lmstudio_models = [m.get("id") for m in lmstudio_data.get("data", [])] if lmstudio_online else []
        local_status["lmstudio"] = {
            "active": lmstudio_online,
            "url": cls.LMSTUDIO_URL,
            "models": lmstudio_models,
        }
        if lmstudio_online:
            active_count += 1

        demo_env = os.getenv("DEMO_MODE")
        if demo_env is not None:
            sandbox_mode = demo_env.strip().lower() in ("true", "1", "yes")
        else:
            sandbox_mode = (active_count == 0)

        return {
            "cloud": cloud_status,
            "local": local_status,
            "active_count": active_count,
            "sandbox_mode": sandbox_mode,
        }

    @classmethod
    def discover_sync(cls) -> Dict[str, Any]:
        """Synchronous wrapper for discovery during CLI or test bootstrap."""
        try:
            return asyncio.run(cls.discover())
        except Exception:
            # Fallback scan without async loop
            env_keys = {
                "openai": bool(os.getenv("OPENAI_API_KEY", "").strip()),
                "anthropic": bool(os.getenv("ANTHROPIC_API_KEY", "").strip()),
                "groq": bool(os.getenv("GROQ_API_KEY", "").strip()),
                "gemini": bool(os.getenv("GEMINI_API_KEY", "").strip()),
                "openrouter": bool(os.getenv("OPENROUTER_API_KEY", "").strip()),
            }
            active = sum(1 for v in env_keys.values() if v)
            return {
                "cloud": {p: {"active": k, "preview": None} for p, k in env_keys.items()},
                "local": {
                    "ollama": {"active": False, "url": cls.OLLAMA_URL, "models": []},
                    "lmstudio": {"active": False, "url": cls.LMSTUDIO_URL, "models": []},
                },
                "active_count": active,
                "sandbox_mode": active == 0,
            }


def format_startup_banner(info: Dict[str, Any], host: str = "127.0.0.1", port: int = 8000) -> str:
    """Generate a clean ASCII box showing active providers, proxy URL, and dashboard link."""
    display_host = "localhost" if host in ("0.0.0.0", "127.0.0.1") else host
    base_url = f"http://{display_host}:{port}/v1"
    dash_url = f"http://{display_host}:{port}/dashboard"

    lines = [
        "┌──────────────────────────────────────────────────────────────────┐",
        "│  SentinelGate v0.3.0 · Zero-Config LLM Gateway                  │",
        "├──────────────────────────────────────────────────────────────────┤",
        f"│  Proxy Endpoint:  {base_url:<46} │",
        f"│  HUD Dashboard:   {dash_url:<46} │",
        "├──────────────────────────────────────────────────────────────────┤",
        "│  Provider Auto-Discovery Inventory:                              │",
    ]

    cloud = info.get("cloud", {})
    for p, data in cloud.items():
        name = p.capitalize()
        if data.get("active"):
            prev = data.get("preview") or "configured"
            lines.append(f"│   ● {name:<11} [Active · {prev:<32}] │")
        else:
            lines.append(f"│   ○ {name:<11} [Not Configured]                               │")

    local = info.get("local", {})
    ollama = local.get("ollama", {})
    if ollama.get("active"):
        models_str = f"({', '.join(ollama.get('models', [])[:2])})" if ollama.get("models") else "(online)"
        lines.append(f"│   ● Ollama       [Local Server: 127.0.0.1:11434 {models_str:<18}] │")
    else:
        lines.append("│   ○ Ollama       [Local Server Offline]                         │")

    lmstudio = local.get("lmstudio", {})
    if lmstudio.get("active"):
        lines.append("│   ● LM Studio    [Local Server: 127.0.0.1:1234 (online)]        │")
    else:
        lines.append("│   ○ LM Studio    [Local Server Offline]                         │")

    lines.append("├──────────────────────────────────────────────────────────────────┤")
    if info.get("sandbox_mode"):
        lines.append("│  Operating Mode: Zero-Key Sandbox (Deterministic Offline Sim)   │")
    else:
        active_num = info.get("active_count", 0)
        lines.append(f"│  Operating Mode: Hybrid Live Gateway ({active_num} providers ready)        │")

    lines.append("│  Smart Router:   model=\"auto\" enabled (sub-ms prompt classifier) │")
    lines.append("│  Secret Shield:  Reversible Redaction firewall active           │")
    lines.append("│  State Engine:   In-Process SQLite (WAL mode + FastEmbed ONNX)   │")
    lines.append("└──────────────────────────────────────────────────────────────────┘")

    return "\n".join(lines)


def print_startup_banner(info: Dict[str, Any], host: str = "127.0.0.1", port: int = 8000):
    print(format_startup_banner(info, host=host, port=port), flush=True)
