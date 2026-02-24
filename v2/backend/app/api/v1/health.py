"""Health check endpoint.

GET /health

Verifies that both backing services (SQLite and QDrant) are reachable and
returns a structured JSON response. The Docker health-check and load
balancers rely on this endpoint.

Response when healthy (HTTP 200):
    {"status": "ok", "sqlite": "ok", "qdrant": "ok"}

Response when degraded (HTTP 503):
    {"status": "error", "sqlite": "ok", "qdrant": "error"}
"""

import asyncio

import aiosqlite
import structlog
from fastapi import APIRouter, Depends, HTTPException
from qdrant_client import QdrantClient

from app.dependencies import get_db, get_qdrant

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check(
    db: aiosqlite.Connection = Depends(get_db),
    qdrant: QdrantClient = Depends(get_qdrant),
) -> dict[str, str]:
    """Return the liveness status of all backing services.

    Runs a trivial query against SQLite and a collections-list call against
    QDrant. If either fails the endpoint returns HTTP 503 so that Docker and
    orchestration tools know to restart the container.
    """
    sqlite_status = await _check_sqlite(db)
    qdrant_status = await _check_qdrant(qdrant)

    overall = "ok" if sqlite_status == "ok" and qdrant_status == "ok" else "error"

    response = {
        "status": overall,
        "sqlite": sqlite_status,
        "qdrant": qdrant_status,
    }

    if overall != "ok":
        logger.warning("health_check_failed", **response)
        raise HTTPException(status_code=503, detail=response)

    return response


async def _check_sqlite(db: aiosqlite.Connection) -> str:
    """Run a trivial SELECT against the open SQLite connection."""
    try:
        async with db.execute("SELECT 1") as cursor:
            await cursor.fetchone()
        return "ok"
    except Exception as exc:
        logger.error("sqlite_health_check_failed", error=str(exc))
        return "error"


async def _check_qdrant(qdrant: QdrantClient) -> str:
    """Call QdrantClient.get_collections() in a thread to avoid blocking."""
    try:
        await asyncio.to_thread(qdrant.get_collections)
        return "ok"
    except Exception as exc:
        logger.error("qdrant_health_check_failed", error=str(exc))
        return "error"
