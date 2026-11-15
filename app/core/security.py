from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi.responses import JSONResponse


def make_openai_error(
    message: str,
    error_type: str = "invalid_request_error",
    code: Optional[str] = None,
    status_code: int = 400,
) -> JSONResponse:
    """Format standard OpenAI REST API error response."""
    payload: Dict[str, Any] = {
        "error": {
            "message": message,
            "type": error_type,
            "param": None,
            "code": code,
        }
    }
    return JSONResponse(status_code=status_code, content=payload)


def parse_bearer_token(authorization_header: Optional[str]) -> Optional[str]:
    if not authorization_header or not authorization_header.startswith("Bearer "):
        return None
    return authorization_header.split("Bearer ", 1)[1].strip()
