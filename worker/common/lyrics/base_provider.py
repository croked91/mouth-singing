"""Abstract base classes for lyrics providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class LyricsCandidate:
    """A lyrics search result candidate before verification."""

    artist: str
    title: str
    lyrics: str
    source: str  # provider name


class TextSearchProvider(ABC):
    """Provider that can search by a text fragment (lyrics snippet)."""

    name: str

    @abstractmethod
    async def search_by_text(self, text_fragment: str) -> list[LyricsCandidate]:
        """Search for lyrics matching *text_fragment*.

        Returns a list of candidates (may be empty). Should never raise on
        "not found" — return ``[]`` instead. May raise
        ``LyricsAPIError`` on infrastructure failures.
        """


class ArtistTitleProvider(ABC):
    """Provider that searches by artist + title metadata."""

    name: str

    @abstractmethod
    async def search_by_metadata(
        self, artist: str, title: str,
    ) -> LyricsCandidate | None:
        """Search for lyrics by *artist* and *title*.

        Returns a candidate or ``None`` if not found. May raise
        ``LyricsAPIError`` on infrastructure failures.
        """
