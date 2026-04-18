"""Lyrics search via multi-provider chain + algorithmic matcher."""

from worker.common.lyrics.base_provider import (
    ArtistTitleProvider,
    LyricsCandidate,
    TextSearchProvider,
)
from worker.common.lyrics.provider_chain import LyricsProviderChain

__all__ = [
    "ArtistTitleProvider",
    "LyricsCandidate",
    "LyricsProviderChain",
    "TextSearchProvider",
]
