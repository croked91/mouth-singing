"""Extract representative text fragments from ASR output for lyrics search."""

from __future__ import annotations

import re


def extract_search_fragments(asr_text: str, n: int = 3) -> list[str]:
    """Pick *n* representative fragments from different parts of the ASR text.

    Each fragment is 5-10 words long, taken from the beginning, middle and end
    of the text so that at least one is likely to match a lyrics database even
    when Whisper introduces errors.

    Returns up to *n* fragments (may be fewer if the text is very short).
    """
    # Split on sentence-like boundaries (., !, ?, newlines, long pauses)
    phrases = re.split(r"[.!?\n]+", asr_text)
    phrases = [p.strip() for p in phrases if p.strip()]

    # If sentence splitting produced nothing useful, fall back to word chunks
    if not phrases:
        words = asr_text.split()
        if len(words) < 3:
            return [asr_text.strip()] if asr_text.strip() else []
        # chunk into groups of ~8 words
        chunk_size = 8
        phrases = [
            " ".join(words[i : i + chunk_size])
            for i in range(0, len(words), chunk_size)
        ]

    # Filter out very short phrases (< 3 words)
    phrases = [p for p in phrases if len(p.split()) >= 3]
    if not phrases:
        # Relax filter — keep everything
        phrases = [p.strip() for p in re.split(r"[.!?\n]+", asr_text) if p.strip()]
        if not phrases:
            return [asr_text.strip()] if asr_text.strip() else []

    # If we have fewer phrases than requested, split long phrases into chunks
    if len(phrases) < n:
        words = asr_text.split()
        if len(words) >= n * 5:
            chunk_size = max(5, min(10, len(words) // n))
            phrases = [
                " ".join(words[i : i + chunk_size])
                for i in range(0, len(words), chunk_size)
            ]

    # Trim each phrase to at most 10 words
    trimmed = [" ".join(p.split()[:10]) for p in phrases]

    if len(trimmed) <= n:
        return trimmed

    # Pick from beginning, middle and end
    indices = _spread_indices(len(trimmed), n)
    return [trimmed[i] for i in indices]


def _spread_indices(length: int, n: int) -> list[int]:
    """Return *n* evenly-spaced indices across [0, length)."""
    if n >= length:
        return list(range(length))
    if n == 1:
        return [0]
    step = (length - 1) / (n - 1)
    return [round(i * step) for i in range(n)]
