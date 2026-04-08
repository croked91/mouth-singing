"""Genius provider — text search via Genius API + lyrics scraping.

Requires GENIUS_TOKEN environment variable. Uses httpx + BeautifulSoup
(no lyricsgenius SDK dependency).
"""

from __future__ import annotations

import httpx
import structlog
from bs4 import BeautifulSoup

from worker.common.lyrics.base_provider import LyricsCandidate, TextSearchProvider

logger = structlog.get_logger(__name__)

_SEARCH_URL = "https://api.genius.com/search"
_MAX_RESULTS = 3
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


class GeniusProvider(TextSearchProvider):
    name = "genius"

    def __init__(self, token: str, timeout: float = 10.0) -> None:
        self._token = token
        self._timeout = timeout

    async def search_by_text(self, text_fragment: str) -> list[LyricsCandidate]:
        headers = {"Authorization": f"Bearer {self._token}"}
        async with httpx.AsyncClient(
            timeout=self._timeout, headers=headers,
        ) as client:
            # Step 1: search
            try:
                resp = await client.get(
                    _SEARCH_URL, params={"q": text_fragment},
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("genius_search_failed", error=str(exc))
                return []

            data = resp.json()
            hits = data.get("response", {}).get("hits", [])
            if not hits:
                return []

            # Step 2: scrape lyrics pages
            candidates: list[LyricsCandidate] = []
            for hit in hits[:_MAX_RESULTS]:
                song = hit.get("result", {})
                url = song.get("url")
                if not url:
                    continue

                lyrics = await self._scrape_lyrics(client, url)
                if not lyrics or len(lyrics) < 20:
                    continue

                candidates.append(LyricsCandidate(
                    artist=song.get("primary_artist", {}).get("name", ""),
                    title=song.get("title", ""),
                    lyrics=lyrics,
                    source=self.name,
                ))

            return candidates

    async def _scrape_lyrics(
        self, client: httpx.AsyncClient, url: str,
    ) -> str:
        try:
            resp = await client.get(
                url,
                headers=_BROWSER_HEADERS,
                follow_redirects=True,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("genius_scrape_failed", url=url, error=str(exc))
            return ""

        soup = BeautifulSoup(resp.text, "html.parser")

        # Genius wraps lyrics in divs with data-lyrics-container="true"
        containers = soup.select("[data-lyrics-container='true']")
        if not containers:
            return ""

        parts: list[str] = []
        for container in containers:
            # Replace <br> with newlines before extracting text
            for br in container.find_all("br"):
                br.replace_with("\n")
            text = container.get_text(separator="\n")
            # Skip header containers (Contributors, Translations, description)
            if not parts and ("Contributors" in text or "Lyrics\n" in text):
                # Strip everything before and including the "Lyrics" marker
                idx = text.find("Lyrics\n")
                if idx != -1:
                    text = text[idx + len("Lyrics\n"):]
                # Remove "Read More" description block
                read_more_idx = text.find("Read More")
                if read_more_idx != -1:
                    text = text[read_more_idx + len("Read More"):]
                text = text.strip()
                if not text:
                    continue
            parts.append(text)

        return "\n".join(parts).strip()
