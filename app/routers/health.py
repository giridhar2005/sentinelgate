from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["Health"])


@router.get("/health")
async def health_check(request: Request):
    cache = getattr(request.app.state, "cache", None)
    model_status = getattr(cache, "model_status", "not_configured") if cache else "not_configured"
    return JSONResponse(
        content={
            "status": "healthy",
            "service": "SentinelGate",
            "version": "0.1.0",
            "embedding_model_status": model_status,
            "timestamp": time.time(),
        }
    )
