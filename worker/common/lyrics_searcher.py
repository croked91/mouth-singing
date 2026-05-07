"""Lyrics search types and helpers shared by search implementations."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class LyricsResult:
    """Structured lyrics search result."""
    artist: str
    title: str
    lyrics: str
    language: str
    confidence: str
    source_note: str


class LyricsSearchError(Exception):
    """Base class for lyrics search errors."""


class LyricsNotFoundError(LyricsSearchError):
    """Song could not be identified or lyrics not found."""


class LyricsAPIError(LyricsSearchError):
    """Network or API error (retryable)."""


def clean_lyrics(raw: str) -> str:
    """Clean scraped lyrics text."""
    # Remove section markers like [Intro], [Verse 1], [Припев: Artist].
    # DOTALL handles multi-line markers (e.g. [Припев: Artist\n& Artist2\n]).
    lyrics = re.sub(r"\[.*?\]\n?", "", raw, flags=re.DOTALL).strip()

    # Genius occasionally inserts <br> before trailing punctuation or before
    # a parenthesised aside, leaving fragments like "Она не твоя\n, ты ..."
    # or "ты? (\nРядом с кем-то\n)". Re-join such wrapped fragments so the
    # downstream aligner does not treat them as separate lines.
    # 1) line starts with closing punctuation → drop the preceding newline(s)
    lyrics = re.sub(r"\n+(?=[ \t]*[,.;:!?)\]…—–])", "", lyrics)
    # 2) line starts with horizontal whitespace → continuation
    lyrics = re.sub(r"\n+(?=[ \t]+\S)", " ", lyrics)
    # 3) previous line ended with opening bracket → continuation
    lyrics = re.sub(r"([(\[])[ \t]*\n+", r"\1", lyrics)

    # Collapse 3+ blank lines into 2.
    lyrics = re.sub(r"\n{3,}", "\n\n", lyrics)

    return lyrics
