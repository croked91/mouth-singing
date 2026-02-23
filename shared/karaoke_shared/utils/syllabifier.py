from __future__ import annotations

import re
import unicodedata

import pyphen
import structlog

from karaoke_shared.models.track import SyllableTiming

logger = structlog.get_logger(__name__)

# Languages supported by pyphen — others fall back to English.
_SUPPORTED_PYPHEN_LANGS = {"en", "ru"}

# Regex that matches only alphabetic characters (Unicode-aware).
_ALPHA_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


class Syllabifier:
    """Splits word-level tokens into syllable-level timings using pyphen.

    pyphen dictionaries are loaded lazily on first use so that constructing
    a Syllabifier is cheap even if pyphen has not been warmed up yet.
    """

    def __init__(self) -> None:
        self._dicts: dict[str, pyphen.Pyphen] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def syllabify(self, tokens: list) -> list[SyllableTiming]:
        """Convert word-level tokens to syllable-level timings.

        For each token the word is split into syllables via pyphen.  The
        token's time span is distributed across syllables in proportion to
        each syllable's character length.  Single-character syllables and
        punctuation are handled gracefully.

        Times in the returned SyllableTiming objects are in SECONDS (float).
        The incoming tokens carry millisecond integers (start_ms / end_ms).

        Args:
            tokens: List of WordToken objects with text, start_ms, end_ms,
                    language fields.

        Returns:
            Flat list of SyllableTiming(syllable, start, end) in seconds.
        """
        result: list[SyllableTiming] = []

        for token in tokens:
            token_start_sec = token.start_ms / 1000.0
            token_end_sec = token.end_ms / 1000.0
            duration_sec = token_end_sec - token_start_sec

            syllables = self._split_word(token.text, token.language)

            if not syllables:
                continue

            if len(syllables) == 1:
                result.append(
                    SyllableTiming(
                        syllable=syllables[0],
                        start=token_start_sec,
                        end=token_end_sec,
                    )
                )
                continue

            # Distribute duration proportionally by character length.
            char_lengths = [len(s) for s in syllables]
            total_chars = sum(char_lengths)

            if total_chars == 0:
                # Fallback: equal distribution.
                equal_share = duration_sec / len(syllables)
                for i, syllable in enumerate(syllables):
                    start = token_start_sec + i * equal_share
                    end = token_start_sec + (i + 1) * equal_share
                    result.append(SyllableTiming(syllable=syllable, start=start, end=end))
                continue

            cursor = token_start_sec
            for i, syllable in enumerate(syllables):
                fraction = char_lengths[i] / total_chars
                syllable_duration = duration_sec * fraction
                end = cursor + syllable_duration
                result.append(SyllableTiming(syllable=syllable, start=cursor, end=end))
                cursor = end

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_dict(self, lang: str | None) -> pyphen.Pyphen:
        """Return a cached pyphen dictionary for the given language tag.

        Falls back to English for unsupported languages.
        """
        # Normalise the BCP-47 tag to a simple two-letter code.
        base_lang = (lang or "en").split("-")[0].lower()

        if base_lang not in _SUPPORTED_PYPHEN_LANGS:
            base_lang = "en"

        if base_lang not in self._dicts:
            pyphen_lang = "ru_RU" if base_lang == "ru" else "en_US"
            self._dicts[base_lang] = pyphen.Pyphen(lang=pyphen_lang)

        return self._dicts[base_lang]

    def _split_word(self, word: str, lang: str | None) -> list[str]:
        """Split a single word string into syllables.

        Only the alphabetic core of the word is syllabified; any leading or
        trailing punctuation / spaces are attached to the nearest syllable.

        Args:
            word: The raw word text from the transcription token.
            lang: BCP-47 language tag, e.g. 'en', 'ru'.

        Returns:
            List of syllable strings; never empty (falls back to [word]).
        """
        text = word.strip()
        if not text:
            return []

        # Find the alphabetic span within the token text.
        match = _ALPHA_RE.search(text)
        if match is None:
            # Pure punctuation — treat the whole token as one syllable.
            return [text]

        alpha_start = match.start()
        alpha_end = match.end()
        prefix = text[:alpha_start]
        alpha_core = text[alpha_start:alpha_end]
        suffix = text[alpha_end:]

        dic = self._get_dict(lang)

        # pyphen.inserted() returns the word with hyphens between syllables.
        inserted = dic.inserted(alpha_core)
        syllable_parts = inserted.split("-")

        if not syllable_parts:
            return [text]

        # Re-attach prefix to the first syllable and suffix to the last.
        syllable_parts[0] = prefix + syllable_parts[0]
        syllable_parts[-1] = syllable_parts[-1] + suffix

        return syllable_parts
