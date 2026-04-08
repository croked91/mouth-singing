"""LRCLib provider — search by artist + title.

API docs: https://lrclib.net/docs
Free, no authentication required.
"""

from __future__ import annotations

import httpx
import structlog

from worker.common.lyrics.base_provider import ArtistTitleProvider, LyricsCandidate

logger = structlog.get_logger(__name__)

_BASE_URL = "https://lrclib.net/api"
_MAX_RESULTS = 3


class LRCLibProvider(ArtistTitleProvider):
    """LRCLib searches by artist+title metadata."""

    name = "lrclib"

    def __init__(self, timeout: float = 10.0) -> None:
        self._timeout = timeout

    async def search_by_metadata(
        self, artist: str, title: str,
    ) -> LyricsCandidate | None:
        transport = httpx.AsyncHTTPTransport(retries=2)
        async with httpx.AsyncClient(
            timeout=self._timeout, transport=transport,
        ) as client:
            # Try combined query first (handles partial artist names better)
            candidates = await self._search(
                client, params={"q": f"{artist} {title}"},
            )
            if not candidates:
                # Fall back to structured search
                candidates = await self._search(
                    client,
                    params={"track_name": title, "artist_name": artist},
                )
            return candidates[0] if candidates else None

    async def _search(
        self, client: httpx.AsyncClient, params: dict,
    ) -> list[LyricsCandidate]:
        try:
            resp = await client.get(f"{_BASE_URL}/search", params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("lrclib_request_failed", error=str(exc))
            return []

        items = resp.json()
        if not items:
            return []

        candidates: list[LyricsCandidate] = []
        for item in items[:_MAX_RESULTS]:
            plain = item.get("plainLyrics") or ""
            if len(plain) >= 20:
                candidates.append(LyricsCandidate(
                    artist=item.get("artistName", ""),
                    title=item.get("trackName", ""),
                    lyrics=plain,
                    source=self.name,
                ))

        return candidates
