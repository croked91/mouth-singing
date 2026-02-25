from __future__ import annotations

import re

import pyphen
import structlog

from karaoke_shared.models.track import SyllableTiming

logger = structlog.get_logger(__name__)

_SUPPORTED_PYPHEN_LANGS = {"en", "ru"}
_ALPHA_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


class Syllabifier:
    """Converts ASR tokens to syllable-level timings for karaoke display.

    Supports two token formats:

    **BPE sub-word tokens** (Soniox): tokens already have sub-word
    granularity, with a leading space marking new words.  These are used
    directly as syllable timings — no further splitting needed.

    **Word-level tokens** (WhisperX): each token is a whole word.  These
    are split into syllables via pyphen with proportional time distribution.

    The format is auto-detected: if *any* token starts with a space the
    input is treated as BPE; otherwise as word-level.
    """

    def __init__(self) -> None:
        self._dicts: dict[str, pyphen.Pyphen] = {}

    def syllabify(self, tokens: list) -> list[SyllableTiming]:
        """Convert tokens to syllable timings.

        Args:
            tokens: Objects with text, start_ms, end_ms, language fields.

        Returns:
            Flat list of SyllableTiming(syllable, start, end) in seconds.
        """
        if not tokens:
            return []

        is_bpe = any(t.text.startswith(" ") for t in tokens)

        if is_bpe:
            return self._from_bpe_tokens(tokens)
        return self._from_word_tokens(tokens)

    # ------------------------------------------------------------------
    # BPE tokens (Soniox) — use as-is
    # ------------------------------------------------------------------

    @staticmethod
    def _from_bpe_tokens(tokens: list) -> list[SyllableTiming]:
        """Convert BPE sub-word tokens directly to syllable timings.

        Leading spaces on tokens are preserved so that rendered karaoke
        text has proper word spacing.
        """
        result: list[SyllableTiming] = []
        for token in tokens:
            text = token.text
            if not text.strip():
                continue
            result.append(SyllableTiming(
                syllable=text,
                start=token.start_ms / 1000.0,
                end=token.end_ms / 1000.0,
            ))
        return result

    # ------------------------------------------------------------------
    # Word-level tokens (WhisperX) — split with pyphen
    # ------------------------------------------------------------------

    def _from_word_tokens(self, tokens: list) -> list[SyllableTiming]:
        """Split word-level tokens into syllables via pyphen."""
        result: list[SyllableTiming] = []

        for idx, token in enumerate(tokens):
            start_sec = token.start_ms / 1000.0
            end_sec = token.end_ms / 1000.0
            duration_sec = end_sec - start_sec

            syllables = self._split_word(token.text, token.language)
            if not syllables:
                continue

            # Add space prefix for non-first words (display spacing).
            if idx > 0:
                syllables[0] = " " + syllables[0]

            if len(syllables) == 1:
                result.append(SyllableTiming(
                    syllable=syllables[0], start=start_sec, end=end_sec,
                ))
                continue

            char_lengths = [max(len(s.strip()), 1) for s in syllables]
            total_chars = sum(char_lengths)
            cursor = start_sec
            for i, syllable in enumerate(syllables):
                fraction = char_lengths[i] / total_chars
                syllable_end = cursor + duration_sec * fraction
                result.append(SyllableTiming(
                    syllable=syllable, start=cursor, end=syllable_end,
                ))
                cursor = syllable_end

        return result

    # ------------------------------------------------------------------
    # Text-only syllabification (no timestamps)
    # ------------------------------------------------------------------

    def split_text_to_syllables(
        self, text: str, language: str
    ) -> tuple[list[str], list[bool]]:
        """Split plain text into syllables without timestamps.

        Useful for pre-alignment: the resulting syllable list can be joined
        with spaces and fed to WhisperX ``force_align()`` so that each
        syllable gets its own timestamp directly from the audio.

        Args:
            text: Plain lyrics text (e.g. ``"любовь не обман"``).
            language: Language code (``"ru"``, ``"en"``).

        Returns:
            A tuple of two equal-length lists:

            - **syllables**: e.g. ``["лю", "бовь", "не", "об", "ман"]``
            - **is_word_start**: ``True`` where a syllable begins a new word,
              e.g. ``[True, False, True, True, False]``
        """
        words = text.split()
        syllables: list[str] = []
        is_word_start: list[bool] = []

        for word in words:
            parts = self._split_word(word, language)
            if not parts:
                continue
            for i, part in enumerate(parts):
                syllables.append(part)
                is_word_start.append(i == 0)

        return syllables, is_word_start

    # ------------------------------------------------------------------
    # Pyphen helpers
    # ------------------------------------------------------------------

    def _get_dict(self, lang: str | None) -> pyphen.Pyphen:
        base_lang = (lang or "en").split("-")[0].lower()
        if base_lang not in _SUPPORTED_PYPHEN_LANGS:
            base_lang = "en"
        if base_lang not in self._dicts:
            pyphen_lang = "ru_RU" if base_lang == "ru" else "en_US"
            self._dicts[base_lang] = pyphen.Pyphen(lang=pyphen_lang)
        return self._dicts[base_lang]

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

        dic = self._get_dict(lang)
        inserted = dic.inserted(alpha_core)
        syllable_parts = inserted.split("-")

        if not syllable_parts:
            return [text]

        syllable_parts[0] = prefix + syllable_parts[0]
        syllable_parts[-1] = syllable_parts[-1] + suffix
        return syllable_parts
