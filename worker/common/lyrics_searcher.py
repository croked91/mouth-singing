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
    lines = raw.split("\n")

    # Skip Genius header noise — find first [Section] marker
    clean_lines: list[str] = []
    started = False
    for line in lines:
        if not started:
            if re.match(r"^\[", line):
                started = True
                clean_lines.append(line)
        else:
            clean_lines.append(line)

    # If no section markers, use all text
    lyrics = "\n".join(clean_lines).strip() if clean_lines else raw

    # Remove section markers like [Intro], [Verse 1], [Припев: Artist]
    # Use DOTALL to handle multi-line markers (e.g. [Припев: Artist\n& Artist2\n])
    lyrics = re.sub(r"\[.*?\]\n?", "", lyrics, flags=re.DOTALL).strip()

    # Collapse 3+ blank lines into 2
    lyrics = re.sub(r"\n{3,}", "\n\n", lyrics)

    return lyrics
