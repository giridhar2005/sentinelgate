from __future__ import annotations

import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(tags=["Health"])


@router.get("/health")
async def health_check():
    return JSONResponse(
        content={
            "status": "healthy",
            "service": "SentinelGate",
            "version": "0.2.0",
            "timestamp": time.time(),
        }
    )
