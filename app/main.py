from __future__ import annotations

import contextlib
import logging
import os
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import AppConfig, load_config
from app.core.database import init_db
from app.routers import dashboard, health, v1
from app.services.budget import BudgetManager, VirtualKeyManager
from app.services.cache import SemanticCache
from app.services.discovery import ProviderDiscovery, print_startup_banner
from app.services.evals import EvalsManager
from app.services.router import ProviderRouter

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("sentinelgate")


def create_app(config_path: Optional[str] = None, db_path: Optional[str] = None) -> FastAPI:
    cfg_file = config_path or os.getenv("SENTINELGATE_CONFIG", "config.yaml")
    config: AppConfig = load_config(cfg_file)
    active_db = db_path or os.getenv("SENTINELGATE_DB", config.cache.db_path)

    # Initialize SQLite schema with WAL mode
    init_db(active_db)

    # Services
    gateway_router = ProviderRouter(config_path=cfg_file)
    cache = SemanticCache(
        db_path=active_db,
        similarity_threshold=config.cache.similarity_threshold,
        default_ttl=config.cache.ttl_seconds,
    )
    evals = EvalsManager(db_path=active_db, sample_rate=config.evals.sample_rate)
    budget = BudgetManager(db_path=active_db, daily_limit_usd=config.budget.daily_limit_usd)
    virtual_keys = VirtualKeyManager(db_path=active_db)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Initializing SentinelGate Gateway engine...")
        try:
            discovery_info = await ProviderDiscovery.discover()
            gateway_router.apply_discovery(discovery_info)
            app.state.discovery_info = discovery_info
            print_startup_banner(discovery_info, host=config.server.host, port=config.server.port)
        except Exception as e:
            logger.warning(f"Provider auto-discovery error: {e}")
        await gateway_router.get_client()
        yield
        logger.info("Gracefully terminating SentinelGate client pool...")
        await gateway_router.close()

    app = FastAPI(
        title="SentinelGate",
        description="Zero-bloat, production-grade OpenAI-compatible LLM Gateway with semantic caching and failover.",
        version="0.3.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
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

    # Include modular sub-routers
    app.include_router(health.router)
    app.include_router(v1.router)
    app.include_router(dashboard.router)

    # Mount compiled React assets if present
    for dist_dir in ["frontend/dist", "/app/frontend/dist"]:
        assets_dir = os.path.join(dist_dir, "assets")
        if os.path.isdir(assets_dir):
            from fastapi.staticfiles import StaticFiles

            app.mount("/assets", StaticFiles(directory=assets_dir), name="frontend_assets")
            break

    @app.get("/", include_in_schema=False)
    async def root_redirect():
        from fastapi.responses import RedirectResponse

        return RedirectResponse(url="/dashboard")

    return app


app = create_app()
