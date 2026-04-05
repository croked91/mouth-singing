"""Health check endpoint.

GET /health

Verifies PostgreSQL is reachable (required) and rec-service is
available (optional — degraded mode is acceptable).

Response when healthy (HTTP 200):
    {"status": "ok", "postgres": "ok", "rec_service": "ok"}

Response when degraded (HTTP 200):
    {"status": "ok", "postgres": "ok", "rec_service": "degraded"}

Response when critical failure (HTTP 503):
    {"status": "error", "postgres": "error", "rec_service": "..."}
"""

import structlog
from fastapi import APIRouter, HTTPException, Request

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check(request: Request) -> dict[str, str]:
    """Return liveness status.

    PostgreSQL is required — if it's down, return 503.
    Rec-service is optional — if it's down, report 'degraded' but still 200.
    """
    pg_status = await _check_postgres(request)
    rec_status = await _check_rec_service(request)

    overall = "ok" if pg_status == "ok" else "error"

    response = {
        "status": overall,
        "postgres": pg_status,
        "rec_service": rec_status,
    }

    if overall != "ok":
        logger.warning("health_check_failed", **response)
        raise HTTPException(status_code=503, detail=response)

    return response


async def _check_postgres(request: Request) -> str:
    try:
        pool = request.app.state.pg_pool
        result = await pool.fetchval("SELECT 1")
        return "ok" if result == 1 else "error"
    except Exception as exc:
        logger.error("postgres_health_check_failed", error=str(exc))
        return "error"


async def _check_rec_service(request: Request) -> str:
    rec_client = getattr(request.app.state, "rec_client", None)
    if rec_client is None:
        return "degraded"
    healthy = await rec_client.health()
    return "ok" if healthy else "degraded"
