"""Lyrics.ovh provider — search by artist + title.

Simple REST API, free, no authentication.
"""

from __future__ import annotations

import httpx
import structlog

from worker.common.lyrics.base_provider import ArtistTitleProvider, LyricsCandidate

logger = structlog.get_logger(__name__)

_BASE_URL = "https://api.lyrics.ovh/v1"


class LyricsOvhProvider(ArtistTitleProvider):
    name = "lyricsovh"

    def __init__(self, timeout: float = 10.0) -> None:
        self._timeout = timeout

    async def search_by_metadata(
        self, artist: str, title: str,
    ) -> LyricsCandidate | None:
        url = f"{_BASE_URL}/{artist}/{title}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("lyricsovh_request_failed", error=str(exc))
                return None

            data = resp.json()
            lyrics = data.get("lyrics", "")
            if len(lyrics) < 20:
                return None

            return LyricsCandidate(
                artist=artist,
                title=title,
                lyrics=lyrics.strip(),
                source=self.name,
            )
