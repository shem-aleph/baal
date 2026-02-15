"""Health check endpoint (no auth required)."""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Query

from sqlalchemy import text

from liberclaw.database.session import get_session_factory

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check(deep: bool = Query(False)):
    """Health check endpoint.

    Basic check returns immediately. Use ?deep=true to verify
    database connectivity and measure latency.
    """
    result = {"status": "ok"}

    if not deep:
        return result

    # Deep check: verify database connectivity
    checks = {}
    try:
        start = time.monotonic()
        async with get_session_factory()() as db:
            await db.execute(text("SELECT 1"))
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        checks["database"] = {"status": "ok", "latency_ms": latency_ms}
    except Exception as e:
        logger.error(f"Health check DB failure: {e}")
        checks["database"] = {"status": "error", "error": str(e)}
        result["status"] = "degraded"

    result["checks"] = checks
    return result
