from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import time
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("sentinelgate.security")

# Server-side pepper for hashing virtual keys at rest
PEPPER = os.getenv("SENTINELGATE_PEPPER", "sg_default_pepper_change_in_production_99a8b7")

# In-memory per-IP auth failure rate limiting
_ip_failures: Dict[str, List[float]] = {}
_LOCKOUT_WINDOW_SECONDS = 60.0
_MAX_AUTH_FAILURES_PER_WINDOW = 10


def get_pepper() -> str:
    return os.getenv("SENTINELGATE_PEPPER", PEPPER)


def hash_virtual_key(raw_key: str) -> str:
    """Hash virtual key with SHA-256 and server-side pepper for safe storage at rest."""
    pepper = get_pepper()
    return hashlib.sha256(f"{pepper}:{raw_key}".encode("utf-8")).hexdigest()


def record_auth_failure(ip: str) -> None:
    now = time.time()
    attempts = _ip_failures.setdefault(ip, [])
    attempts.append(now)
    _ip_failures[ip] = [t for t in attempts if now - t < _LOCKOUT_WINDOW_SECONDS]


def is_ip_locked_out(ip: str) -> bool:
    now = time.time()
    attempts = _ip_failures.get(ip, [])
    active = [t for t in attempts if now - t < _LOCKOUT_WINDOW_SECONDS]
    _ip_failures[ip] = active
    return len(active) >= _MAX_AUTH_FAILURES_PER_WINDOW


def reset_ip_failures(ip: str) -> None:
    _ip_failures.pop(ip, None)


def parse_bearer_token(authorization_header: Optional[str]) -> Optional[str]:
    if not authorization_header or not authorization_header.startswith("Bearer "):
        return None
    return authorization_header.split("Bearer ", 1)[1].strip()


def extract_token(request: Request) -> Optional[str]:
    # 1. Authorization: Bearer <token>
    auth_header = request.headers.get("authorization") or request.headers.get("Authorization")
    token = parse_bearer_token(auth_header)
    if token:
        return token

    # 2. x-api-key: <token>
    api_key_header = request.headers.get("x-api-key") or request.headers.get("X-Api-Key")
    if api_key_header:
        return api_key_header.strip()

    # 3. Query parameters (for browser UI access like /dashboard)
    query_token = request.query_params.get("token") or request.query_params.get("key")
    if query_token:
        return query_token.strip()

    return None


class AuthContext:
    def __init__(
        self,
        token: str,
        is_admin: bool = False,
        virtual_key: Optional[Dict[str, Any]] = None,
        rate_limit_headers: Optional[Dict[str, str]] = None,
    ):
        self.token = token
        self.is_admin = is_admin
        self.virtual_key = virtual_key or {}
        self.rate_limit_headers = rate_limit_headers or {}


async def verify_auth(request: Request) -> AuthContext:
    """Unified FastAPI authentication dependency applied to every route except /health."""
    # Exempt /health and static favicon
    if request.url.path in ("/health", "/favicon.ico"):
        return AuthContext(token="public", is_admin=False)

    client_ip = request.client.host if request.client else "127.0.0.1"

    # Per-IP rate limiting on auth failures
    if is_ip_locked_out(client_ip):
        raise HTTPException(
            status_code=429,
            detail="Too many failed authentication attempts. Please wait before retrying.",
        )

    # Check Keyless Demo Mode
    is_demo = os.getenv("DEMO_MODE", "").lower() == "true"
    if is_demo:
        # Refuse if not bound to 127.0.0.1 / localhost
        server_host = getattr(request.app.state.config.server, "host", "127.0.0.1")
        if server_host not in ("127.0.0.1", "localhost"):
            raise HTTPException(
                status_code=500,
                detail="Keyless demo mode can only be bound to 127.0.0.1 or localhost.",
            )

        # In demo mode on localhost, allow keyless access or standard sentinel-local
        token = extract_token(request)
        if not token or token in ("sentinel-local", "demo"):
            return AuthContext(token="demo", is_admin=True)

    # In production/standard mode, token is strictly required
    token = extract_token(request)
    if not token:
        record_auth_failure(client_ip)
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Provide Authorization: Bearer <token> or x-api-key header.",
        )

    # 1. Constant-time comparison against configured admin token or local dev key
    admin_token = getattr(request.app.state.config.server, "admin_token", "") or os.getenv(
        "SENTINELGATE_ADMIN_TOKEN", ""
    )
    server_host = getattr(request.app.state.config.server, "host", "127.0.0.1")
    if (admin_token and secrets.compare_digest(token, admin_token)) or (
        server_host in ("127.0.0.1", "localhost")
        and secrets.compare_digest(token, "sentinel-local")
    ):
        reset_ip_failures(client_ip)
        return AuthContext(token="admin", is_admin=True)

    # 2. Check virtual key manager (hashed at rest with pepper)
    virtual_keys = getattr(request.app.state, "virtual_keys", None)
    if virtual_keys:
        estimated_tokens = 10
        if request.method == "POST" and request.url.path in (
            "/v1/chat/completions",
            "/v1/messages",
        ):
            try:
                body = await request.json()
                if isinstance(body, dict):
                    msgs = body.get("messages", [])
                    user_prompt = ""
                    for m in reversed(msgs):
                        if isinstance(m, dict) and m.get("role") == "user":
                            content = m.get("content", "")
                            if isinstance(content, list):
                                user_prompt = "".join(
                                    b.get("text", "") for b in content if isinstance(b, dict)
                                )
                            else:
                                user_prompt = str(content)
                            break
                    if user_prompt:
                        estimated_tokens = max(1, len(user_prompt) // 4)
            except Exception:
                pass

        is_valid, err_msg, rl_hdrs = virtual_keys.verify_and_rate_limit(
            token, estimated_tokens=estimated_tokens
        )
        if is_valid:
            reset_ip_failures(client_ip)
            request.state.rate_limit_headers = rl_hdrs
            return AuthContext(
                token=token,
                is_admin=False,
                virtual_key={"token": token},
                rate_limit_headers=rl_hdrs,
            )
        else:
            record_auth_failure(client_ip)
            status_code = 429 if "rate limit" in (err_msg or "").lower() else 401
            raise HTTPException(
                status_code=status_code,
                detail=err_msg or "Invalid authentication token.",
                headers=rl_hdrs,
            )

    record_auth_failure(client_ip)
    raise HTTPException(status_code=401, detail="Invalid authentication token.")


class RequestBodyLimitMiddleware(BaseHTTPMiddleware):
    """Enforces request body size limits on all incoming HTTP requests."""

    def __init__(self, app, max_body_size: int = 10 * 1024 * 1024):
        super().__init__(app)
        self.max_body_size = max_body_size

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > self.max_body_size:
                    return JSONResponse(
                        status_code=413,
                        content={
                            "error": {
                                "message": f"Payload too large. Maximum allowed request size is {self.max_body_size} bytes.",
                                "type": "payload_too_large",
                            }
                        },
                    )
            except ValueError:
                pass
        return await call_next(request)


class LogSanitizationFilter(logging.Filter):
    """Prevents API keys, authorization tokens, and credentials from ever appearing in logs."""

    PATTERNS = [
        re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]+", re.IGNORECASE),
        re.compile(r"sg-live-[A-Za-z0-9_\-]+", re.IGNORECASE),
        re.compile(r"sk-[A-Za-z0-9_\-]+", re.IGNORECASE),
        re.compile(r"x-api-key['\":\s]+[A-Za-z0-9_\-]+", re.IGNORECASE),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            for pattern in self.PATTERNS:
                record.msg = pattern.sub("[REDACTED_CREDENTIAL]", record.msg)
        return True


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
