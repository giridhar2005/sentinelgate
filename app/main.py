from __future__ import annotations

import contextlib
import logging
import os
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from app.core.config import AppConfig, load_config
from app.core.database import init_db
from app.core.security import (
    LogSanitizationFilter,
    RequestBodyLimitMiddleware,
    verify_auth,
)
from app.routers import dashboard, health, mcp, metrics, v1
from app.services.budget import BudgetManager, VirtualKeyManager
from app.services.cache import SemanticCache
from app.services.discovery import ProviderDiscovery, print_startup_banner
from app.services.evals import EvalsManager
from app.services.gateway_pipeline import GatewayPipeline
from app.services.router import ProviderRouter

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("sentinelgate")
logger.addFilter(LogSanitizationFilter())


def check_demo_mode_constraints(config: AppConfig) -> None:
    if os.getenv("DEMO_MODE", "").lower() != "true":
        return
    host = getattr(config.server, "host", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError(
            f"Keyless demo mode (--demo) can only be bound to 127.0.0.1 or localhost. Current host: {host}"
        )

    real_keys = []
    for env_var in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY"]:
        val = os.getenv(env_var, "").strip()
        if (
            val
            and not val.startswith("${")
            and val not in ("mock", "test", "key-alpha", "key-beta", "demo")
        ):
            real_keys.append(env_var)

    for name, provider in config.providers.items():
        for key in provider.api_keys:
            key_str = key.strip()
            if (
                key_str
                and not key_str.startswith("${")
                and key_str
                not in ("mock", "test", "key-alpha", "key-beta", "groq-key-1", "ant-key-1", "demo")
            ):
                if key_str.startswith("sk-") or len(key_str) >= 20:
                    real_keys.append(f"{name} api_key")

    if real_keys:
        raise ValueError(
            f"Keyless demo mode refused to start: real provider keys detected in environment/config ({', '.join(real_keys)}). "
            "Cannot run unauthenticated demo mode with active credentials."
        )

    logger.warning("=" * 78)
    logger.warning("! [SECURITY WARNING] RUNNING IN KEYLESS ZERO-KEY DEMO MODE !".center(76))
    logger.warning(
        "! Bound to 127.0.0.1. Unauthenticated local access enabled. Never expose to network !".center(
            76
        )
    )
    logger.warning("=" * 78)


def create_app(config_path: Optional[str] = None, db_path: Optional[str] = None) -> FastAPI:
    cfg_file: str = (
        config_path if config_path is not None else os.getenv("SENTINELGATE_CONFIG", "config.yaml")
    )
    config: AppConfig = load_config(cfg_file)
    active_db: str = (
        db_path
        if db_path is not None
        else (os.getenv("SENTINELGATE_DB") or config.cache.db_path or "sentinelgate.db")
    )

    # Initialize SQLite schema with WAL mode
    init_db(active_db)

    # Services
    gateway_router = ProviderRouter(config_path=cfg_file)
    cache = SemanticCache(
        db_path=active_db,
        similarity_threshold=config.cache.similarity_threshold,
        default_ttl=config.cache.ttl_seconds,
        exact_match_fallback=config.cache.exact_match_fallback,
    )
    evals = EvalsManager(db_path=active_db, sample_rate=config.evals.sample_rate)
    budget = BudgetManager(db_path=active_db, daily_limit_usd=config.budget.daily_limit_usd)
    virtual_keys = VirtualKeyManager(db_path=active_db)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Initializing SentinelGate Gateway engine...")
        evals.pipeline.start()
        try:
            discovery_info = await ProviderDiscovery.discover()
            gateway_router.apply_discovery(discovery_info)
            app.state.discovery_info = discovery_info
            print_startup_banner(discovery_info, host=config.server.host, port=config.server.port)
        except Exception as e:
            logger.warning(f"Provider auto-discovery error: {e}")
        await gateway_router.get_client()
        yield
        logger.info("Gracefully terminating SentinelGate client pool and telemetry pipeline...")
        await gateway_router.close()
        await evals.pipeline.stop()

    check_demo_mode_constraints(config)

    app = FastAPI(
        title="SentinelGate",
        description="Zero-bloat, production-grade OpenAI-compatible LLM Gateway with semantic caching and failover.",
        version="0.5.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(verify_auth)],
    )

    @app.get("/openapi.json", include_in_schema=False)
    async def get_openapi_endpoint():
        from fastapi.openapi.utils import get_openapi
        from fastapi.responses import JSONResponse

        return JSONResponse(get_openapi(title=app.title, version=app.version, routes=app.routes))

    @app.get("/docs", include_in_schema=False)
    async def get_docs_endpoint():
        from fastapi.openapi.docs import get_swagger_ui_html

        return get_swagger_ui_html(
            openapi_url="/openapi.json", title=app.title + " - Documentation"
        )

    @app.get("/redoc", include_in_schema=False)
    async def get_redoc_endpoint():
        from fastapi.openapi.docs import get_redoc_html

        return get_redoc_html(openapi_url="/openapi.json", title=app.title + " - ReDoc")

    @app.exception_handler(HTTPException)
    async def custom_http_exception_handler(request: Request, exc: HTTPException):
        if request.url.path.startswith("/v1/"):
            code = (
                "rate_limit_exceeded"
                if exc.status_code == 429
                else ("invalid_api_key" if exc.status_code == 401 else None)
            )
            error_type = "auth_error" if exc.status_code == 401 else "invalid_request_error"
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "error": {
                        "message": exc.detail,
                        "type": error_type,
                        "param": None,
                        "code": code,
                    }
                },
                headers=getattr(exc, "headers", None) or {},
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None) or {},
        )

    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_body_size=config.server.max_body_size_bytes,
    )

    if config.server.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.server.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Attach instances to app state
    app.state.config = config
    app.state.router = gateway_router
    app.state.cache = cache
    app.state.evals = evals
    app.state.budget = budget
    app.state.virtual_keys = virtual_keys
    pipeline = GatewayPipeline(
        router=gateway_router,
        cache=cache,
        budget=budget,
        evals=evals,
        virtual_keys=virtual_keys,
        config=config,
    )
    app.state.pipeline = pipeline

    # Include modular sub-routers
    app.include_router(health.router)
    app.include_router(v1.router)
    app.include_router(mcp.router)
    app.include_router(dashboard.router)
    app.include_router(metrics.router)

    # Mount compiled React assets if present
    for dist_dir in ["frontend/dist", "/app/frontend/dist"]:
        assets_dir = os.path.join(dist_dir, "assets")
        if os.path.isdir(assets_dir):
            from fastapi.staticfiles import StaticFiles

            app.mount("/assets", StaticFiles(directory=assets_dir), name="frontend_assets")
            break

    @app.get("/", include_in_schema=False)
    async def root_redirect():

        return RedirectResponse(url="/dashboard")

    return app


app = create_app()
