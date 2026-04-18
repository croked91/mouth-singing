"""Text normalization for ASR↔lyrics matching.

Produces a list of ``WordFeatures`` from raw text. Steps:

1. Unicode NFKC + lowercase.
2. Strip structural markers (``[Verse]``, ``[Припев]``, …) and chord notations.
3. Drop short bracketed content (often ad-libs / backing vocals).
4. Strip punctuation except the apostrophe (kept for ``don't``).
5. Tokenize on whitespace, featurize each token.

The normalizer deliberately does NOT collapse consecutive duplicate words —
song hooks and chorus repetitions are legitimate (e.g. "белые розы белые розы").
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from worker.common.lyrics.matching.linguistics import WordFeatures, make_word_featurizer

# [Section: ...] / [Куплет 1] / [Chorus] / [Bridge] etc.
_SECTION_RE = re.compile(r"\[[^\[\]]*\]", re.UNICODE)

# (single short word in parens) — typical ad-lib / backing vocal annotation.
# Keeps long parenthetical content (often part of lyrics).
_SHORT_PARENS_RE = re.compile(r"\(([^()]{1,30})\)")

# Anything that is not a letter, digit, apostrophe or whitespace.
_PUNCT_RE = re.compile(r"[^\w'\s]+", re.UNICODE)

# Standalone digit runs — drop (lyrics with track numbers, year, etc.).
_DIGIT_RUN_RE = re.compile(r"\b\d+\b", re.UNICODE)


@dataclass(frozen=True)
class NormalizedText:
    text: str
    words: tuple[WordFeatures, ...]

    @property
    def word_count(self) -> int:
        return len(self.words)


def normalize_text(text: str, language: str) -> NormalizedText:
    cleaned = _clean_text(text)
    featurize = make_word_featurizer(language)
    tokens = [t for t in cleaned.split() if t]
    words = tuple(featurize(t) for t in tokens if t)
    # Drop tokens that featurized to empty (e.g. punctuation-only tokens).
    words = tuple(w for w in words if w.text)
    return NormalizedText(text=cleaned, words=words)


def _clean_text(text: str) -> str:
    if not text:
        return ""
    # 1. Unicode normalize + lowercase.
    cleaned = unicodedata.normalize("NFKC", text).lower()
    # 2. Strip section markers and chord notations.
    cleaned = _SECTION_RE.sub(" ", cleaned)
    # 3. Drop short parenthesized content (ad-libs).
    cleaned = _SHORT_PARENS_RE.sub(" ", cleaned)
    # 4. Drop standalone digit tokens.
    cleaned = _DIGIT_RUN_RE.sub(" ", cleaned)
    # 5. Strip punctuation except apostrophes and whitespace.
    cleaned = _PUNCT_RE.sub(" ", cleaned)
    # 6. Collapse whitespace.
    cleaned = " ".join(cleaned.split())
    return cleaned
