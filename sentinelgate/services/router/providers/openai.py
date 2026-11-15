from __future__ import annotations

from typing import Any, Dict, Tuple


def prepare_openai_request(
    provider_type: str,
    base_url: str,
    api_key: str,
    request_data: Dict[str, Any],
    model: str,
) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    """Build URL, headers, and payload for OpenAI, Groq, and Ollama providers."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = dict(request_data)
    payload.pop("_headers", None)
    payload["model"] = model
    return url, headers, payload
