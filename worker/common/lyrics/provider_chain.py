"""Lyrics provider chain — orchestrates multi-provider search + matching.

Flow:
1. Parse filename via DeepSeek (if available) to get artist/title hints.
2. Extract fragments from ASR text.
3. Run text-search providers (by fragments) + metadata providers (by hints)
   in parallel.
4. Pass collected candidates through ``LyricsMatcher`` for selection.
5. If no candidate passes the matcher, fall back to ``LyricsAgent`` (web
   search) which returns more candidates — pass those through the same
   matcher.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from worker.common.lyrics.base_provider import (
    ArtistTitleProvider,
    LyricsCandidate,
    TextSearchProvider,
)
from worker.common.lyrics.fragments import extract_search_fragments
from worker.common.lyrics_searcher import (
    LyricsNotFoundError,
    LyricsResult,
)

if TYPE_CHECKING:
    from worker.common.lyrics.filename_parser import FilenameParser
    from worker.common.lyrics.matching import LyricsMatcher
    from worker.common.lyrics_agent import LyricsAgent

logger = structlog.get_logger(__name__)


class LyricsProviderChain:
    """Orchestrates lyrics search across multiple providers + matcher."""

    def __init__(
        self,
        text_providers: list[TextSearchProvider],
        metadata_providers: list[ArtistTitleProvider],
        matcher: LyricsMatcher | None = None,
        filename_parser: FilenameParser | None = None,
        fallback_agent: LyricsAgent | None = None,
        search_fragments: int = 3,
    ) -> None:
        self._text_providers = text_providers
        self._metadata_providers = metadata_providers
        self._matcher = matcher
        self._filename_parser = filename_parser
        self._fallback_agent = fallback_agent
        self._search_fragments = search_fragments

    async def search(
        self,
        asr_text: str,
        detected_language: str,
        artist_hint: str | None = None,
        title_hint: str | None = None,
        filename: str | None = None,
    ) -> LyricsResult:
        """Search for lyrics, match against ASR, optionally fall back to web."""
        t0 = time.monotonic()

        # ------------------------------------------------------------------
        # Stage 0: parse filename for hints (if we don't have them yet).
        # Filename parser may return alternative spellings (e.g. Latin
        # ``Dzetta`` alongside canonical ``Джетта``) — both are tried by
        # downstream providers and forwarded to the fallback agent.
        # ------------------------------------------------------------------
        artist_alts: list[str] = []
        title_alts: list[str] = []
        if filename and (not artist_hint or not title_hint) and self._filename_parser:
            parsed = await self._filename_parser.parse(filename)
            if not artist_hint:
                artist_hint = parsed.artist
                artist_alts = parsed.artist_alts
            if not title_hint:
                title_hint = parsed.title
                title_alts = parsed.title_alts
            if artist_hint or title_hint:
                logger.info(
                    "lyrics_filename_parsed",
                    filename=filename,
                    artist=artist_hint,
                    title=title_hint,
                    artist_alts=artist_alts or None,
                    title_alts=title_alts or None,
                )

        # ------------------------------------------------------------------
        # Stage 1: collect candidates from providers (parallel)
        # ------------------------------------------------------------------
        candidates = await self._collect_candidates(
            asr_text, artist_hint, title_hint,
            artist_alts=artist_alts, title_alts=title_alts,
        )
        candidates = _deduplicate(candidates)
        logger.info(
            "lyrics_candidates_collected",
            count=len(candidates),
            sources=[c.source for c in candidates],
            elapsed=round(time.monotonic() - t0, 2),
        )

        artist_variants = _variants(artist_hint, artist_alts)
        title_variants = _variants(title_hint, title_alts)

        # ------------------------------------------------------------------
        # Stage 2: matcher
        # ------------------------------------------------------------------
        if candidates and self._matcher:
            result = await self._matcher.match(
                asr_text, candidates, detected_language,
                artist_hints=artist_variants,
                title_hints=title_variants,
            )
            if result:
                logger.info(
                    "lyrics_matched",
                    artist=result.artist,
                    title=result.title,
                    source=result.source_note,
                    confidence=result.confidence,
                    elapsed=round(time.monotonic() - t0, 2),
                )
                return result
            logger.info("lyrics_match_rejected_all", count=len(candidates))

        # ------------------------------------------------------------------
        # Stage 3: Fallback — agent collects more candidates → matcher again
        # ------------------------------------------------------------------
        if self._fallback_agent:
            logger.info("lyrics_fallback_to_agent")
            agent_candidates = await self._fallback_agent.search(
                asr_text, detected_language, artist_hint, title_hint,
                artist_alts=artist_alts, title_alts=title_alts,
            )
            agent_candidates = _deduplicate(agent_candidates)
            logger.info(
                "lyrics_agent_candidates",
                count=len(agent_candidates),
            )
            if agent_candidates and self._matcher:
                result = await self._matcher.match(
                    asr_text, agent_candidates, detected_language,
                    artist_hints=artist_variants,
                    title_hints=title_variants,
                )
                if result:
                    logger.info(
                        "lyrics_matched_after_agent",
                        artist=result.artist,
                        title=result.title,
                        elapsed=round(time.monotonic() - t0, 2),
                    )
                    return result

        # ------------------------------------------------------------------
        # Stage 4: ASR fallback — neither providers nor agent found a usable
        # candidate. Rather than failing the whole pipeline, hand the raw
        # Whisper transcription downstream as the lyrics. CTC alignment will
        # still produce per-word timings against what was actually sung, even
        # if the text contains ASR errors. Better than no track at all.
        # ------------------------------------------------------------------
        asr_clean = (asr_text or "").strip()
        if len(asr_clean) >= 20:
            logger.warning(
                "lyrics_using_asr_fallback",
                asr_chars=len(asr_clean),
                artist_hint=artist_hint,
                title_hint=title_hint,
                elapsed=round(time.monotonic() - t0, 2),
            )
            return LyricsResult(
                artist=artist_hint or "Unknown",
                title=title_hint or "Unknown",
                lyrics=asr_clean,
                language=detected_language,
                confidence="low",
                source_note="asr_fallback",
            )

        raise LyricsNotFoundError(
            "No candidates passed the matcher and ASR transcription is too "
            "short to use as fallback"
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _collect_candidates(
        self,
        asr_text: str,
        artist_hint: str | None,
        title_hint: str | None,
        artist_alts: list[str] | None = None,
        title_alts: list[str] | None = None,
    ) -> list[LyricsCandidate]:
        fragments = extract_search_fragments(asr_text, n=self._search_fragments)
        tasks: list[asyncio.Task] = []

        artist_variants = _variants(artist_hint, artist_alts)
        title_variants = _variants(title_hint, title_alts)

        for provider in self._text_providers:
            for fragment in fragments:
                tasks.append(
                    asyncio.create_task(
                        _safe_text_search(provider, fragment),
                        name=f"{provider.name}:{fragment[:30]}",
                    )
                )

        if artist_variants and title_variants:
            for a in artist_variants:
                for t in title_variants:
                    combined = f"{a} {t}"
                    for provider in self._text_providers:
                        tasks.append(
                            asyncio.create_task(
                                _safe_text_search(provider, combined),
                                name=f"{provider.name}:combined:{combined[:30]}",
                            )
                        )
        elif title_variants:
            for t in title_variants:
                for provider in self._text_providers:
                    tasks.append(
                        asyncio.create_task(
                            _safe_text_search(provider, t),
                            name=f"{provider.name}:title:{t[:30]}",
                        )
                    )

        if artist_variants and title_variants:
            for a in artist_variants:
                for t in title_variants:
                    for provider in self._metadata_providers:
                        tasks.append(
                            asyncio.create_task(
                                _safe_metadata_search(provider, a, t),
                                name=f"{provider.name}:meta:{a[:20]}/{t[:20]}",
                            )
                        )

        if not tasks:
            return []

        results = await asyncio.gather(*tasks, return_exceptions=True)

        candidates: list[LyricsCandidate] = []
        for r in results:
            if isinstance(r, list):
                candidates.extend(r)
            elif isinstance(r, LyricsCandidate):
                candidates.append(r)

        return candidates


async def _safe_text_search(
    provider: TextSearchProvider,
    fragment: str,
) -> list[LyricsCandidate]:
    try:
        return await provider.search_by_text(fragment)
    except Exception as exc:
        logger.warning(
            "text_provider_error", provider=provider.name, error=str(exc),
        )
        return []


async def _safe_metadata_search(
    provider: ArtistTitleProvider,
    artist: str,
    title: str,
) -> LyricsCandidate | None:
    try:
        return await provider.search_by_metadata(artist, title)
    except Exception as exc:
        logger.warning(
            "metadata_provider_error", provider=provider.name, error=str(exc),
        )
        return None


def _deduplicate(candidates: list[LyricsCandidate]) -> list[LyricsCandidate]:
    seen: set[tuple[str, str]] = set()
    unique: list[LyricsCandidate] = []
    for c in candidates:
        key = (c.artist.lower().strip(), c.title.lower().strip())
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


def _variants(primary: str | None, alts: list[str] | None) -> list[str]:
    """Return primary + non-empty unique alternatives, preserving order."""
    out: list[str] = []
    seen: set[str] = set()
    for v in (primary, *(alts or [])):
        if not v:
            continue
        key = v.casefold().strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out
