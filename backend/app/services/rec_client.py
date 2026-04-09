"""HTTP client for the rec-service microservice.

All methods return None on failure (timeout, connection error, non-2xx)
so the caller can fall back to popular strategy.
"""

from __future__ import annotations

import httpx
import structlog

logger = structlog.get_logger(__name__)


class RecClient:
    """Async HTTP client wrapping calls to rec-service."""

    def __init__(self, base_url: str, timeout: float = 5.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def get_recommendations(
        self,
        played_track_ids: list[str],
        limit: int = 5,
        language: str | None = None,
        exclude_ids: list[str] | None = None,
    ) -> dict | None:
        """Call POST /recommendations. Returns response dict or None on failure."""
        body: dict = {
            "played_track_ids": played_track_ids,
            "limit": limit,
        }
        if language:
            body["language"] = language
        if exclude_ids:
            body["exclude_ids"] = exclude_ids

        return await self._post("/recommendations", body)

    async def get_tag_recommendations(
        self,
        tag_id: int,
        played_track_ids: list[str],
        limit: int = 5,
        language: str | None = None,
    ) -> dict | None:
        """Call POST /recommendations/by-tag. Returns response dict or None."""
        body: dict = {
            "tag_id": tag_id,
            "played_track_ids": played_track_ids,
            "limit": limit,
        }
        if language:
            body["language"] = language

        return await self._post("/recommendations/by-tag", body)

    async def get_tags(
        self,
        played_track_ids: list[str],
        limit: int = 8,
    ) -> list[dict] | None:
        """Call POST /tags. Returns list of tags or None."""
        body = {"played_track_ids": played_track_ids, "limit": limit}
        return await self._post("/tags", body)

    async def mood_search(
        self,
        query_text: str,
        limit: int = 50,
    ) -> dict | None:
        """Call POST /search/mood. Returns {items: [...]} or None.

        Uses a longer timeout because the first call may trigger lazy
        model loading in the rec-service (~10-30s).
        """
        return await self._post("/search/mood", {"query_text": query_text, "limit": limit}, timeout=30.0)

    async def health(self) -> bool:
        """Check rec-service health."""
        try:
            resp = await self._client.get("/health")
            return resp.status_code == 200
        except Exception:
            return False

    async def _post(self, path: str, body: dict, timeout: float | None = None) -> dict | list | None:
        try:
            resp = await self._client.post(path, json=body, **({"timeout": timeout} if timeout else {}))
            if resp.status_code == 200:
                return resp.json()
            logger.warning("rec_client.non_200", path=path, status=resp.status_code)
            return None
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            logger.warning("rec_client.unavailable", path=path, error=str(exc))
            return None
        except Exception as exc:
            logger.warning("rec_client.error", path=path, error=str(exc))
            return None
