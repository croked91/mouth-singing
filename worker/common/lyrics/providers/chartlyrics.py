"""ChartLyrics provider — text search via XML API.

Free, no authentication. HTTP only (no HTTPS).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import httpx
import structlog

from worker.common.lyrics.base_provider import LyricsCandidate, TextSearchProvider

logger = structlog.get_logger(__name__)

_SEARCH_URL = "http://api.chartlyrics.com/apiv1.asmx/SearchLyricText"
_GET_URL = "http://api.chartlyrics.com/apiv1.asmx/GetLyric"
_MAX_RESULTS = 3


class ChartLyricsProvider(TextSearchProvider):
    name = "chartlyrics"

    def __init__(self, timeout: float = 10.0) -> None:
        self._timeout = timeout

    async def search_by_text(self, text_fragment: str) -> list[LyricsCandidate]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            # Step 1: search by lyric text
            try:
                resp = await client.get(
                    _SEARCH_URL, params={"lyricText": text_fragment},
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("chartlyrics_search_failed", error=str(exc))
                return []

            try:
                root = ET.fromstring(resp.text)
            except ET.ParseError:
                logger.warning("chartlyrics_xml_parse_error")
                return []

            ns = _detect_namespace(root)

            entries = root.findall(f".//{ns}SearchLyricResult")
            if not entries:
                return []

            # Step 2: fetch full lyrics for top results
            candidates: list[LyricsCandidate] = []
            for entry in entries[:_MAX_RESULTS]:
                lyric_id = _text(entry, f"{ns}LyricId")
                checksum = _text(entry, f"{ns}LyricChecksum")
                if not lyric_id or lyric_id == "0" or not checksum:
                    continue

                artist = _text(entry, f"{ns}Artist") or ""
                title = _text(entry, f"{ns}Song") or ""

                candidate = await self._fetch_full(
                    client, lyric_id, checksum, artist, title,
                )
                if candidate:
                    candidates.append(candidate)

            return candidates

    async def _fetch_full(
        self,
        client: httpx.AsyncClient,
        lyric_id: str,
        checksum: str,
        artist: str,
        title: str,
    ) -> LyricsCandidate | None:
        try:
            resp = await client.get(
                _GET_URL,
                params={"lyricId": lyric_id, "lyricCheckSum": checksum},
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            return None

        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError:
            return None

        ns = _detect_namespace(root)
        lyrics = _text(root, f"{ns}Lyric") or ""
        if len(lyrics) < 20:
            return None

        return LyricsCandidate(
            artist=_text(root, f"{ns}LyricArtist") or artist,
            title=_text(root, f"{ns}LyricSong") or title,
            lyrics=lyrics.strip(),
            source=self.name,
        )


def _detect_namespace(root: ET.Element) -> str:
    """Extract XML namespace prefix from the root tag, if any."""
    tag = root.tag
    if tag.startswith("{"):
        return tag[: tag.index("}") + 1]
    return ""


def _text(el: ET.Element, path: str) -> str:
    """Get text content of a child element, or empty string."""
    child = el.find(path)
    return (child.text or "").strip() if child is not None else ""
