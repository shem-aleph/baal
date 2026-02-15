"""LiberClaw API server — FastAPI app with lifespan, CORS, router mounting."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from sqlalchemy import select, update

from liberclaw.auth.dependencies import set_settings
from liberclaw.config import LiberClawSettings
from liberclaw.database.session import close_engine, get_session_factory, init_engine
from liberclaw.routers import activity, agents, auth, chat, files, health, network, templates, usage, users

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def _reset_stuck_agents() -> None:
    """Reset agents stuck in deploying status from previous shutdown."""
    from liberclaw.database.models import Agent

    async with get_session_factory()() as db:
        result = await db.execute(
            select(Agent.id, Agent.name).where(Agent.deployment_status == "deploying")
        )
        stuck_agents = result.all()
        
        if not stuck_agents:
            return

        await db.execute(
            update(Agent)
            .where(Agent.deployment_status == "deploying")
            .values(deployment_status="failed")
        )
        await db.commit()
        
        for agent_id, name in stuck_agents:
            logger.warning(f"Reset stuck agent '{name}' ({agent_id}) from deploying → failed")


async def _initialize_vm_pool(settings: LiberClawSettings) -> VMPool | None:
    """Initialize VM pool if enabled, return None on failure."""
    if not settings.pool_enabled:
        return None

    try:
        from baal_core.deployer import AlephDeployer
        from baal_core.pool_manager import VMPool

        deployer = AlephDeployer(
            private_key=settings.aleph_private_key,
            ssh_pubkey=settings.aleph_ssh_pubkey,
            ssh_privkey_path=settings.aleph_ssh_privkey_path,
        )
        
        pool = VMPool(
            db_path=settings.pool_db_path,
            deployer=deployer,
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
            replenish_interval=settings.pool_replenish_interval,
            max_age_hours=settings.pool_max_age_hours,
        )
        
        await pool.initialize()
        await pool.start_replenisher()
        
        logger.info(f"VM pool enabled (min={settings.pool_min_size}, max={settings.pool_max_size})")
        return pool
        
    except Exception as e:
        logger.error(f"VM pool initialization failed, continuing without pool: {e}")
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    settings: LiberClawSettings = app.state.settings

    # Initialize database
    init_engine(settings)
    logger.info("Database engine initialized")

    # Store settings for auth dependencies
    set_settings(settings)

    # Reset agents stuck in "deploying" from previous shutdown
    await _reset_stuck_agents()

    # Initialize VM pool for instant agent deployment
    app.state.vm_pool = await _initialize_vm_pool(settings)

    yield

    # Shutdown
    pool = getattr(app.state, "vm_pool", None)
    if pool:
        await pool.close()
        logger.info("VM pool closed")
        
    await close_engine()
    logger.info("Database engine closed")


def create_app(settings: LiberClawSettings | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    if settings is None:
        settings = LiberClawSettings()

    app = FastAPI(
        title="LiberClaw API",
        description="AI agent management platform on Aleph Cloud",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings

    # Rate limiting
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded

    from liberclaw.rate_limit import limiter

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Mount routers under /api/v1
    app.include_router(health.router, prefix="/api/v1")
    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(agents.router, prefix="/api/v1")
    app.include_router(templates.router, prefix="/api/v1")
    app.include_router(templates.skills_router, prefix="/api/v1")
    app.include_router(chat.router, prefix="/api/v1")
    app.include_router(files.router, prefix="/api/v1")
    app.include_router(users.router, prefix="/api/v1")
    app.include_router(usage.router, prefix="/api/v1")
    app.include_router(network.router, prefix="/api/v1")
    app.include_router(activity.router, prefix="/api/v1")

    return app


# Default app instance for `uvicorn liberclaw.main:app`
app = create_app()
