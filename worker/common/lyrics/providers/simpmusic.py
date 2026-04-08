"""SimpMusic provider — text search via YouTube Music backend.

Free, no authentication required.
"""

from __future__ import annotations

import httpx
import structlog

from worker.common.lyrics.base_provider import LyricsCandidate, TextSearchProvider

logger = structlog.get_logger(__name__)

_BASE_URL = "https://api-lyrics.simpmusic.org/v1"
_MAX_RESULTS = 3


class SimpMusicProvider(TextSearchProvider):
    name = "simpmusic"

    def __init__(self, timeout: float = 10.0) -> None:
        self._timeout = timeout

    async def search_by_text(self, text_fragment: str) -> list[LyricsCandidate]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            # Step 1: search
            try:
                resp = await client.get(
                    f"{_BASE_URL}/search", params={"q": text_fragment},
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("simpmusic_search_failed", error=str(exc))
                return []

            data = resp.json()
            items = data.get("data") or data.get("items") or []
            if not items:
                return []

            # Step 2: fetch lyrics for top results
            candidates: list[LyricsCandidate] = []
            for item in items[:_MAX_RESULTS]:
                video_id = item.get("videoId") or item.get("id")
                if not video_id:
                    continue
                candidate = await self._fetch_lyrics(client, video_id, item)
                if candidate:
                    candidates.append(candidate)

            return candidates

    async def _fetch_lyrics(
        self, client: httpx.AsyncClient, video_id: str, meta: dict,
    ) -> LyricsCandidate | None:
        try:
            resp = await client.get(f"{_BASE_URL}/{video_id}")
            resp.raise_for_status()
        except httpx.HTTPError:
            return None

        data = resp.json()
        lyrics = (
            data.get("plainLyrics")
            or data.get("lyrics")
            or ""
        )
        if len(lyrics) < 20:
            return None

        return LyricsCandidate(
            artist=meta.get("artist", meta.get("artists", "")),
            title=meta.get("title", meta.get("name", "")),
            lyrics=lyrics.strip(),
            source=self.name,
        )
