"""Per-word linguistic features for ASR↔lyrics matching.

Each word is represented as a triple of features:

* ``text`` — original lowercased word.
* ``lemma`` — morphological normal form (pymorphy3 for RU, snowball stem for EN
  and other supported languages, otherwise the lowercased text).
* ``skeleton`` — consonant skeleton (vowels and soft/hard signs dropped, then
  ``unidecode``-ed). Tolerant to vowel substitutions and morphological endings,
  which are the dominant Whisper errors in singing.
* ``metaphone`` — Metaphone phonetic code for Latin-script words; empty for
  Cyrillic and other non-Latin scripts (skeleton already covers them).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

import jellyfish
import pymorphy3
import snowballstemmer
import structlog
from unidecode import unidecode

logger = structlog.get_logger(__name__)

_ENGLISH_VOWELS = frozenset("aeiouy")
_RU_DROP_CHARS = frozenset("аеёиоуыэюяьъй")

_MORPH: pymorphy3.MorphAnalyzer | None = None
_MORPH_LOCK = threading.Lock()


@dataclass(frozen=True)
class WordFeatures:
    text: str
    lemma: str
    skeleton: str
    metaphone: str


WordFeaturizer = Callable[[str], WordFeatures]


def make_word_featurizer(language: str) -> WordFeaturizer:
    lang = (language or "").lower().strip()
    if lang == "ru":
        return _ru_featurizer()
    if lang == "en":
        return _en_featurizer()
    return _universal_featurizer(lang)


def init_morph_analyzer() -> pymorphy3.MorphAnalyzer:
    """Eagerly initialise the shared pymorphy3 MorphAnalyzer.

    Construction reads ~30 MB of dictionary data and takes ~1–2 s. Call
    once from worker startup (before consume) so the first Russian-language
    job does not block the event loop on lazy init.

    Thread-safe: a process-wide lock guarantees a single MorphAnalyzer
    instance even if multiple threads race here.
    """
    global _MORPH
    if _MORPH is not None:
        return _MORPH
    with _MORPH_LOCK:
        if _MORPH is None:
            logger.info("pymorphy3_init_start")
            _MORPH = pymorphy3.MorphAnalyzer()
            logger.info("pymorphy3_init_done")
    return _MORPH


def _shared_morph() -> pymorphy3.MorphAnalyzer:
    return init_morph_analyzer()


def _ru_featurizer() -> WordFeaturizer:
    morph = _shared_morph()

    def featurize(word: str) -> WordFeatures:
        norm = word.lower().strip()
        if not norm:
            return _EMPTY
        try:
            lemma = morph.parse(norm)[0].normal_form
        except Exception:
            lemma = norm
        return WordFeatures(
            text=norm,
            lemma=lemma,
            skeleton=_skeleton_cyrillic(norm),
            metaphone="",
        )

    return featurize


def _en_featurizer() -> WordFeaturizer:
    stemmer = snowballstemmer.stemmer("english")

    def featurize(word: str) -> WordFeatures:
        norm = word.lower().strip()
        if not norm:
            return _EMPTY
        return WordFeatures(
            text=norm,
            lemma=stemmer.stemWord(norm),
            skeleton=_skeleton_latin(norm),
            metaphone=_safe_metaphone(norm),
        )

    return featurize


def _universal_featurizer(language: str) -> WordFeaturizer:
    """Featurizer for unknown / mixed languages.

    Tries snowballstemmer for the given language; falls back to the
    lowercased text. Skeleton goes through ``unidecode`` first so that any
    script reduces to a Latin consonant skeleton.
    """
    stemmer = None
    if language:
        try:
            stemmer = snowballstemmer.stemmer(language)
        except (KeyError, ValueError):
            stemmer = None

    def featurize(word: str) -> WordFeatures:
        norm = word.lower().strip()
        if not norm:
            return _EMPTY
        lemma = stemmer.stemWord(norm) if stemmer else norm
        ascii_form = unidecode(norm)
        return WordFeatures(
            text=norm,
            lemma=lemma,
            skeleton=_skeleton_latin(ascii_form),
            metaphone=_safe_metaphone(ascii_form) if ascii_form.isascii() else "",
        )

    return featurize


def _skeleton_cyrillic(word: str) -> str:
    """Consonant skeleton for Cyrillic — drop vowels/soft-hard signs, then unidecode."""
    filtered = "".join(c for c in word if c.isalpha() and c not in _RU_DROP_CHARS)
    return unidecode(filtered).replace("'", "").lower()


def _skeleton_latin(word: str) -> str:
    """Consonant skeleton for Latin-script words."""
    return "".join(
        c for c in word if c.isalpha() and c.lower() not in _ENGLISH_VOWELS
    ).lower()


def _safe_metaphone(word: str) -> str:
    if not word:
        return ""
    try:
        return jellyfish.metaphone(word)
    except Exception:
        return ""


_EMPTY = WordFeatures(text="", lemma="", skeleton="", metaphone="")
