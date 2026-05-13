from __future__ import annotations

import re

import pyphen
import structlog

logger = structlog.get_logger(__name__)

_SUPPORTED_PYPHEN_LANGS = {"en", "ru"}
_ALPHA_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")


class Syllabifier:
    """Splits words into syllables via pyphen.

    Used by CTC aligners to produce per-syllable timings from
    word-level alignments via proportional time distribution.
    """

    def __init__(self) -> None:
        self._dicts: dict[str, pyphen.Pyphen] = {}

    def _get_dict(self, lang: str | None) -> pyphen.Pyphen:
        base_lang = (lang or "en").split("-")[0].lower()
        if base_lang not in _SUPPORTED_PYPHEN_LANGS:
            base_lang = "en"
        if base_lang not in self._dicts:
            pyphen_lang = "ru_RU" if base_lang == "ru" else "en_US"
            self._dicts[base_lang] = pyphen.Pyphen(lang=pyphen_lang)
        return self._dicts[base_lang]

    @staticmethod
    def _detect_word_lang(word: str) -> str:
        """Detect language of a single word by its script.

        Returns ``"ru"`` if the word contains any Cyrillic character,
        ``"en"`` otherwise.  This allows correct pyphen dictionary
        selection even when ``--language ru`` is used globally but
        the track contains English lyrics.
        """
        return "ru" if _CYRILLIC_RE.search(word) else "en"

    def _split_word(self, word: str, lang: str | None) -> list[str]:
        text = word.strip()
        if not text:
            return []

        match = _ALPHA_RE.search(text)
        if match is None:
            return [text]

        alpha_start = match.start()
        all_alpha = list(_ALPHA_RE.finditer(text))
        alpha_end = all_alpha[-1].end()
        prefix = text[:alpha_start]
        alpha_core = text[alpha_start:alpha_end]
        suffix = text[alpha_end:]

        # Use per-word script detection so English words get the en_US
        # pyphen dictionary even when the global language is "ru".
        effective_lang = self._detect_word_lang(alpha_core)
        dic = self._get_dict(effective_lang)
        inserted = dic.inserted(alpha_core)
        syllable_parts = inserted.split("-")

        if not syllable_parts:
            return [text]

        syllable_parts[0] = prefix + syllable_parts[0]
        syllable_parts[-1] = syllable_parts[-1] + suffix
        return syllable_parts
