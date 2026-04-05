"""Health check endpoint.

GET /health

Verifies that both backing services (PostgreSQL and QDrant) are reachable and
returns a structured JSON response. The Docker health-check and load
balancers rely on this endpoint.

Response when healthy (HTTP 200):
    {"status": "ok", "postgres": "ok", "qdrant": "ok"}

Response when degraded (HTTP 503):
    {"status": "error", "postgres": "ok", "qdrant": "error"}
"""

import asyncio

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from qdrant_client import QdrantClient

from app.dependencies import get_qdrant

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check(
    request: Request,
    qdrant: QdrantClient = Depends(get_qdrant),
) -> dict[str, str]:
    """Return the liveness status of all backing services.

    Runs a trivial query against PostgreSQL and a collections-list call against
    QDrant. If either fails the endpoint returns HTTP 503 so that Docker and
    orchestration tools know to restart the container.
    """
    pg_status = await _check_postgres(request)
    qdrant_status = await _check_qdrant(qdrant)

    overall = "ok" if pg_status == "ok" and qdrant_status == "ok" else "error"

    response = {
        "status": overall,
        "postgres": pg_status,
        "qdrant": qdrant_status,
    }

    if overall != "ok":
        logger.warning("health_check_failed", **response)
        raise HTTPException(status_code=503, detail=response)

    return response


async def _check_postgres(request: Request) -> str:
    """Run a trivial SELECT against the PostgreSQL pool."""
    try:
        pool = request.app.state.pg_pool
        result = await pool.fetchval("SELECT 1")
        return "ok" if result == 1 else "error"
    except Exception as exc:
        logger.error("postgres_health_check_failed", error=str(exc))
        return "error"


async def _check_qdrant(qdrant: QdrantClient) -> str:
    """Call QdrantClient.get_collections() in a thread to avoid blocking."""
    try:
        await asyncio.to_thread(qdrant.get_collections)
        return "ok"
    except Exception as exc:
        logger.error("qdrant_health_check_failed", error=str(exc))
        return "error"
