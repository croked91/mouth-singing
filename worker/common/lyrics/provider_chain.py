"""Lyrics provider chain — orchestrates multi-provider search + verification.

Flow:
1. Parse filename via DeepSeek (if available) to get artist/title hints
2. Extract fragments from ASR text
3. Search text-search providers (by fragments) + metadata providers (by hints)
4. DeepSeek verifies candidates
5. Fallback to DeepSeek agent + Yandex Search if nothing matched
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
    from worker.common.lyrics.verifier import LyricsVerifier
    from worker.common.lyrics_agent import LyricsAgent

logger = structlog.get_logger(__name__)


class LyricsProviderChain:
    """Orchestrates lyrics search across multiple providers.

    Same ``search()`` signature as ``LyricsAgent`` — drop-in replacement.
    """

    def __init__(
        self,
        text_providers: list[TextSearchProvider],
        metadata_providers: list[ArtistTitleProvider],
        verifier: LyricsVerifier | None = None,
        fallback_agent: LyricsAgent | None = None,
        search_fragments: int = 3,
    ) -> None:
        self._text_providers = text_providers
        self._metadata_providers = metadata_providers
        self._verifier = verifier
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
        """Search for lyrics using providers, then verify, then fallback.

        Raises:
            LyricsNotFoundError: If no provider and no fallback found lyrics.
            LyricsAPIError: On infrastructure failures.
        """
        t0 = time.monotonic()

        # ------------------------------------------------------------------
        # Stage 0: parse filename for hints (if we don't have them yet)
        # ------------------------------------------------------------------
        if filename and (not artist_hint or not title_hint) and self._verifier:
            parsed_artist, parsed_title = await self._verifier.parse_filename(
                filename,
            )
            artist_hint = artist_hint or parsed_artist
            title_hint = title_hint or parsed_title
            if artist_hint or title_hint:
                logger.info(
                    "lyrics_filename_parsed",
                    filename=filename,
                    artist=artist_hint,
                    title=title_hint,
                )

        # ------------------------------------------------------------------
        # Stage 1: collect candidates from providers (parallel)
        # ------------------------------------------------------------------
        candidates = await self._collect_candidates(
            asr_text,
            artist_hint,
            title_hint,
        )
        logger.info(
            "lyrics_candidates_collected",
            count=len(candidates),
            sources=[c.source for c in candidates],
            elapsed=round(time.monotonic() - t0, 2),
        )

        # Deduplicate by (artist_lower, title_lower)
        candidates = _deduplicate(candidates)

        # ------------------------------------------------------------------
        # Stage 2: DeepSeek verification
        # ------------------------------------------------------------------
        if candidates and self._verifier:
            result = await self._verifier.verify(
                asr_text,
                candidates,
                detected_language,
            )
            if result:
                logger.info(
                    "lyrics_verified",
                    artist=result.artist,
                    title=result.title,
                    source=result.source_note,
                    elapsed=round(time.monotonic() - t0, 2),
                )
                return result
            logger.info("lyrics_verification_rejected_all")

        # ------------------------------------------------------------------
        # Stage 3: Fallback — DeepSeek agent + web search
        # ------------------------------------------------------------------
        if self._fallback_agent:
            logger.info("lyrics_fallback_to_agent")
            return await self._fallback_agent.search(
                asr_text,
                detected_language,
                artist_hint,
                title_hint,
            )

        raise LyricsNotFoundError(
            "No candidates found and no fallback agent configured"
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _collect_candidates(
        self,
        asr_text: str,
        artist_hint: str | None,
        title_hint: str | None,
    ) -> list[LyricsCandidate]:
        """Run all providers in parallel and collect candidates."""
        fragments = extract_search_fragments(asr_text, n=self._search_fragments)
        tasks: list[asyncio.Task] = []

        # Text-search providers: one task per (provider, fragment)
        for provider in self._text_providers:
            for fragment in fragments:
                tasks.append(
                    asyncio.create_task(
                        _safe_text_search(provider, fragment),
                        name=f"{provider.name}:{fragment[:30]}",
                    )
                )

        # Text-search by "artist title" combined (most precise)
        if artist_hint and title_hint:
            combined = f"{artist_hint} {title_hint}"
            for provider in self._text_providers:
                tasks.append(
                    asyncio.create_task(
                        _safe_text_search(provider, combined),
                        name=f"{provider.name}:combined:{combined[:30]}",
                    )
                )
        # Text-search by title only (fallback if no artist)
        elif title_hint:
            for provider in self._text_providers:
                tasks.append(
                    asyncio.create_task(
                        _safe_text_search(provider, title_hint),
                        name=f"{provider.name}:title:{title_hint[:30]}",
                    )
                )

        # Metadata providers: only if we have both hints
        if artist_hint and title_hint:
            for provider in self._metadata_providers:
                tasks.append(
                    asyncio.create_task(
                        _safe_metadata_search(provider, artist_hint, title_hint),
                        name=f"{provider.name}:meta",
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
            # Exceptions and None are silently ignored

        return candidates


async def _safe_text_search(
    provider: TextSearchProvider,
    fragment: str,
) -> list[LyricsCandidate]:
    """Call provider.search_by_text, catching all exceptions."""
    try:
        return await provider.search_by_text(fragment)
    except Exception as exc:
        logger.warning(
            "text_provider_error",
            provider=provider.name,
            error=str(exc),
        )
        return []


async def _safe_metadata_search(
    provider: ArtistTitleProvider,
    artist: str,
    title: str,
) -> LyricsCandidate | None:
    """Call provider.search_by_metadata, catching all exceptions."""
    try:
        return await provider.search_by_metadata(artist, title)
    except Exception as exc:
        logger.warning(
            "metadata_provider_error",
            provider=provider.name,
            error=str(exc),
        )
        return None


def _deduplicate(candidates: list[LyricsCandidate]) -> list[LyricsCandidate]:
    """Remove duplicate candidates by (artist, title) — keep first seen."""
    seen: set[tuple[str, str]] = set()
    unique: list[LyricsCandidate] = []
    for c in candidates:
        key = (c.artist.lower().strip(), c.title.lower().strip())
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique
