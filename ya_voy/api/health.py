"""``GET /health``: liveness plus Postgres and Redis connectivity (used by Dokku healthchecks)."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ya_voy.api.dependencies import HealthCheck, get_health_checks

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])

CHECK_TIMEOUT_SECONDS = 2.0


async def _run(name: str, check: HealthCheck) -> tuple[str, bool]:
    try:
        await asyncio.wait_for(check(), timeout=CHECK_TIMEOUT_SECONDS)
    except Exception:
        logger.warning("health check failed", extra={"check": name}, exc_info=True)
        return name, False
    return name, True


@router.get("/health")
async def health(
    checks: Annotated[dict[str, HealthCheck], Depends(get_health_checks)],
) -> JSONResponse:
    results = dict(await asyncio.gather(*(_run(n, c) for n, c in checks.items())))
    healthy = all(results.values())
    body = {
        "status": "ok" if healthy else "error",
        "checks": {name: "ok" if ok else "error" for name, ok in results.items()},
    }
    return JSONResponse(body, status_code=200 if healthy else 503)
