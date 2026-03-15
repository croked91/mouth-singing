"""Unit tests for Syllabifier.

Strategy
--------
- Uses a FakeWordToken to avoid importing the worker package's WordToken model.
- All tests are synchronous since Syllabifier has no async methods.
- Table-driven tests via pytest.mark.parametrize for the proportional-timing
  and language-fallback scenarios.
- Each test verifies both correctness (syllable text) and timing properties
  (ms→seconds conversion, continuity, proportionality).
"""

from __future__ import annotations

import pytest

from karaoke_shared.models.track import SyllableTiming
from karaoke_shared.utils.syllabifier import Syllabifier


# ---------------------------------------------------------------------------
# Minimal stub – mirrors the WordToken interface without importing worker code
# ---------------------------------------------------------------------------


class FakeWordToken:
    """Minimal stand-in for a word-token model (no worker imports needed)."""

    def __init__(
        self,
        text: str,
        start_ms: int,
        end_ms: int,
        language: str | None = None,
    ) -> None:
        self.text = text
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.language = language


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _syl(syllabifier: Syllabifier, token: FakeWordToken) -> list[SyllableTiming]:
    """Convenience: syllabify a single token."""
    return syllabifier.syllabify([token])


# ---------------------------------------------------------------------------
# Single-syllable words
# ---------------------------------------------------------------------------


class TestSingleSyllableWords:
    """A single-syllable word must produce exactly one SyllableTiming that
    spans the full token duration."""

    @pytest.mark.parametrize(
        "word, start_ms, end_ms",
        [
            ("cat", 0, 500),
            ("dog", 1000, 1300),
            ("the", 200, 400),
            ("a", 50, 150),
            ("world", 0, 800),
        ],
    )
    def test_single_syllable_covers_full_span(
        self, word: str, start_ms: int, end_ms: int
    ) -> None:
        syl = Syllabifier()
        token = FakeWordToken(word, start_ms, end_ms, language="en")

        result = _syl(syl, token)

        assert len(result) == 1
        assert result[0].syllable == word
        assert result[0].start == pytest.approx(start_ms / 1000.0)
        assert result[0].end == pytest.approx(end_ms / 1000.0)


# ---------------------------------------------------------------------------
# Multi-syllable English words
# ---------------------------------------------------------------------------


class TestMultiSyllableEnglish:
    """pyphen splits 'beautiful' → ['beau', 'ti', 'ful'] (3 parts).

    Time is distributed proportionally by character length.
    'beau' = 4 chars, 'ti' = 2 chars, 'ful' = 3 chars → total 9 chars.
    """

    def test_beautiful_produces_three_syllables(self) -> None:
        syl = Syllabifier()
        token = FakeWordToken("beautiful", start_ms=0, end_ms=900, language="en")

        result = syl.syllabify([token])

        assert len(result) == 3

    def test_beautiful_syllable_texts(self) -> None:
        syl = Syllabifier()
        token = FakeWordToken("beautiful", start_ms=0, end_ms=900, language="en")

        result = syl.syllabify([token])

        assert result[0].syllable == "beau"
        assert result[1].syllable == "ti"
        assert result[2].syllable == "ful"

    def test_beautiful_proportional_timing(self) -> None:
        """Duration is shared proportionally: beau=4/9, ti=2/9, ful=3/9."""
        syl = Syllabifier()
        # 900 ms → 0.9 s total
        token = FakeWordToken("beautiful", start_ms=0, end_ms=900, language="en")
        result = syl.syllabify([token])

        total_chars = 4 + 2 + 3  # 9
        duration = 0.9

        assert result[0].start == pytest.approx(0.0)
        assert result[0].end == pytest.approx(duration * 4 / total_chars)

        assert result[1].start == pytest.approx(duration * 4 / total_chars)
        assert result[1].end == pytest.approx(duration * (4 + 2) / total_chars)

        assert result[2].start == pytest.approx(duration * (4 + 2) / total_chars)
        assert result[2].end == pytest.approx(duration)

    def test_hello_produces_two_syllables(self) -> None:
        """pyphen splits 'hello' → ['hel', 'lo']."""
        syl = Syllabifier()
        token = FakeWordToken("hello", start_ms=0, end_ms=600, language="en")

        result = syl.syllabify([token])

        assert len(result) == 2
        assert result[0].syllable == "hel"
        assert result[1].syllable == "lo"

    def test_hello_time_conversion_ms_to_seconds(self) -> None:
        """Token milliseconds are converted to float seconds in output."""
        syl = Syllabifier()
        token = FakeWordToken("hello", start_ms=1500, end_ms=2100, language="en")

        result = syl.syllabify([token])

        assert result[0].start == pytest.approx(1.5)
        assert result[-1].end == pytest.approx(2.1)


# ---------------------------------------------------------------------------
# Time conversion: ms → seconds
# ---------------------------------------------------------------------------


class TestMillisecondsToSeconds:
    """Verify the ms → s conversion is applied consistently."""

    def test_start_in_seconds(self) -> None:
        syl = Syllabifier()
        token = FakeWordToken("cat", start_ms=2500, end_ms=3000, language="en")

        result = _syl(syl, token)

        assert result[0].start == pytest.approx(2.5)

    def test_end_in_seconds(self) -> None:
        syl = Syllabifier()
        token = FakeWordToken("cat", start_ms=0, end_ms=750, language="en")

        result = _syl(syl, token)

        assert result[0].end == pytest.approx(0.75)

    def test_large_ms_values(self) -> None:
        syl = Syllabifier()
        token = FakeWordToken("cat", start_ms=120000, end_ms=120500, language="en")

        result = _syl(syl, token)

        assert result[0].start == pytest.approx(120.0)
        assert result[0].end == pytest.approx(120.5)


# ---------------------------------------------------------------------------
# Continuity: no gaps between consecutive syllables in one word
# ---------------------------------------------------------------------------


class TestSyllableContinuity:
    """Within a single word the end of syllable[i] equals the start of
    syllable[i+1] — no gaps or overlaps are allowed."""

    @pytest.mark.parametrize(
        "word",
        ["beautiful", "programming", "hello", "computer"],
    )
    def test_no_gaps_between_syllables(self, word: str) -> None:
        syl = Syllabifier()
        token = FakeWordToken(word, start_ms=0, end_ms=1000, language="en")

        result = syl.syllabify([token])

        for i in range(len(result) - 1):
            assert result[i].end == pytest.approx(result[i + 1].start), (
                f"Gap found between syllable {i} and {i + 1} in '{word}'"
            )

    @pytest.mark.parametrize(
        "word",
        ["beautiful", "programming", "hello", "computer"],
    )
    def test_first_syllable_starts_at_token_start(self, word: str) -> None:
        syl = Syllabifier()
        token = FakeWordToken(word, start_ms=500, end_ms=1500, language="en")

        result = syl.syllabify([token])

        assert result[0].start == pytest.approx(0.5)

    @pytest.mark.parametrize(
        "word",
        ["beautiful", "programming", "hello", "computer"],
    )
    def test_last_syllable_ends_at_token_end(self, word: str) -> None:
        syl = Syllabifier()
        token = FakeWordToken(word, start_ms=500, end_ms=1500, language="en")

        result = syl.syllabify([token])

        assert result[-1].end == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------


class TestEmptyInput:
    def test_empty_tokens_list_returns_empty(self) -> None:
        syl = Syllabifier()

        result = syl.syllabify([])

        assert result == []


# ---------------------------------------------------------------------------
# Pure punctuation
# ---------------------------------------------------------------------------


class TestPunctuationTokens:
    """Pure punctuation has no alphabetic core and is treated as one syllable."""

    @pytest.mark.parametrize(
        "punct",
        [",", ".", "!", "?", "...", "—", "--"],
    )
    def test_pure_punctuation_becomes_single_syllable(self, punct: str) -> None:
        syl = Syllabifier()
        token = FakeWordToken(punct, start_ms=0, end_ms=100, language="en")

        result = _syl(syl, token)

        assert len(result) == 1
        assert result[0].syllable == punct

    @pytest.mark.parametrize(
        "punct",
        [",", ".", "!"],
    )
    def test_punctuation_preserves_full_span(self, punct: str) -> None:
        syl = Syllabifier()
        token = FakeWordToken(punct, start_ms=500, end_ms=700, language="en")

        result = _syl(syl, token)

        assert result[0].start == pytest.approx(0.5)
        assert result[0].end == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# Words with attached punctuation
# ---------------------------------------------------------------------------


class TestWordsWithPunctuation:
    """Leading/trailing punctuation must be preserved and attached to the
    nearest syllable, not dropped."""

    def test_trailing_comma_attached_to_last_syllable(self) -> None:
        """'Hello,' → last syllable is 'lo,' not 'lo'."""
        syl = Syllabifier()
        token = FakeWordToken("Hello,", start_ms=0, end_ms=600, language="en")

        result = syl.syllabify([token])

        assert result[-1].syllable.endswith(",")

    def test_trailing_period_attached_to_last_syllable(self) -> None:
        syl = Syllabifier()
        token = FakeWordToken("world.", start_ms=0, end_ms=500, language="en")

        result = syl.syllabify([token])

        assert result[-1].syllable.endswith(".")

    def test_leading_quote_attached_to_first_syllable(self) -> None:
        """'"Hello' → first syllable starts with '"'."""
        syl = Syllabifier()
        token = FakeWordToken('"Hello', start_ms=0, end_ms=600, language="en")

        result = syl.syllabify([token])

        assert result[0].syllable.startswith('"')

    def test_word_with_punctuation_covers_full_span(self) -> None:
        syl = Syllabifier()
        token = FakeWordToken("Hello,", start_ms=200, end_ms=800, language="en")

        result = syl.syllabify([token])

        assert result[0].start == pytest.approx(0.2)
        assert result[-1].end == pytest.approx(0.8)

    def test_exclamation_mark_attached(self) -> None:
        syl = Syllabifier()
        token = FakeWordToken("wow!", start_ms=0, end_ms=400, language="en")

        result = syl.syllabify([token])

        assert result[-1].syllable.endswith("!")


# ---------------------------------------------------------------------------
# Russian language syllabification
# ---------------------------------------------------------------------------


class TestRussianSyllabification:
    """pyphen supports Russian (ru_RU dictionary).

    'привет' → ['при', 'вет'] (2 syllables)
    'компьютер' → ['ком', 'пью', 'тер'] (3 syllables)
    """

    def test_privet_two_syllables(self) -> None:
        syl = Syllabifier()
        token = FakeWordToken("привет", start_ms=0, end_ms=600, language="ru")

        result = syl.syllabify([token])

        assert len(result) == 2

    def test_privet_syllable_texts(self) -> None:
        syl = Syllabifier()
        token = FakeWordToken("привет", start_ms=0, end_ms=600, language="ru")

        result = syl.syllabify([token])

        assert result[0].syllable == "при"
        assert result[1].syllable == "вет"

    def test_kompyuter_three_syllables(self) -> None:
        syl = Syllabifier()
        token = FakeWordToken("компьютер", start_ms=0, end_ms=900, language="ru")

        result = syl.syllabify([token])

        assert len(result) == 3

    def test_russian_token_timing_ms_to_seconds(self) -> None:
        syl = Syllabifier()
        token = FakeWordToken("привет", start_ms=3000, end_ms=3600, language="ru")

        result = syl.syllabify([token])

        assert result[0].start == pytest.approx(3.0)
        assert result[-1].end == pytest.approx(3.6)

    def test_russian_continuity(self) -> None:
        syl = Syllabifier()
        token = FakeWordToken("компьютер", start_ms=0, end_ms=900, language="ru")

        result = syl.syllabify([token])

        for i in range(len(result) - 1):
            assert result[i].end == pytest.approx(result[i + 1].start)


# ---------------------------------------------------------------------------
# Unknown language fallback → English
# ---------------------------------------------------------------------------


class TestUnknownLanguageFallback:
    """An unrecognised language tag must silently fall back to English."""

    @pytest.mark.parametrize(
        "lang_tag",
        [None, "xx", "zh", "fr", "de", "ja", "es"],
    )
    def test_unknown_lang_does_not_raise(self, lang_tag: str | None) -> None:
        syl = Syllabifier()
        token = FakeWordToken("hello", start_ms=0, end_ms=500, language=lang_tag)

        # Should not raise
        result = syl.syllabify([token])

        assert len(result) >= 1

    def test_none_language_falls_back_to_english(self) -> None:
        """language=None uses English; 'hello' → 2 syllables."""
        syl = Syllabifier()
        en_token = FakeWordToken("hello", start_ms=0, end_ms=500, language="en")
        none_token = FakeWordToken("hello", start_ms=0, end_ms=500, language=None)

        result_en = syl.syllabify([en_token])
        result_none = syl.syllabify([none_token])

        assert [t.syllable for t in result_en] == [t.syllable for t in result_none]

    def test_bcp47_subtag_stripped(self) -> None:
        """'en-US' should be treated the same as 'en'."""
        syl = Syllabifier()
        token_bcp = FakeWordToken("hello", start_ms=0, end_ms=500, language="en-US")
        token_simple = FakeWordToken("hello", start_ms=0, end_ms=500, language="en")

        result_bcp = syl.syllabify([token_bcp])
        result_simple = syl.syllabify([token_simple])

        assert [t.syllable for t in result_bcp] == [t.syllable for t in result_simple]


# ---------------------------------------------------------------------------
# Mixed-language tokens
# ---------------------------------------------------------------------------


class TestMixedLanguageTokens:
    """A list with both English and Russian tokens must produce correct results
    for each token according to its own language tag."""

    def test_mixed_lang_correct_syllable_count(self) -> None:
        syl = Syllabifier()
        tokens = [
            FakeWordToken("hello", start_ms=0, end_ms=600, language="en"),
            FakeWordToken("привет", start_ms=700, end_ms=1300, language="ru"),
            FakeWordToken("world", start_ms=1400, end_ms=1800, language="en"),
        ]

        result = syl.syllabify(tokens)

        # 'hello'=2 + 'привет'=2 + 'world'=1 = 5
        assert len(result) == 5

    def test_mixed_lang_syllable_texts(self) -> None:
        syl = Syllabifier()
        tokens = [
            FakeWordToken("hello", start_ms=0, end_ms=600, language="en"),
            FakeWordToken("привет", start_ms=700, end_ms=1300, language="ru"),
        ]

        result = syl.syllabify(tokens)

        syllables = [t.syllable for t in result]
        assert "hel" in syllables
        assert "lo" in syllables
        # Non-first words get a leading space for display purposes.
        assert " при" in syllables or "при" in syllables
        assert "вет" in syllables

    def test_mixed_lang_timing_is_independent(self) -> None:
        """Timings from different tokens are independent — no cross-contamination."""
        syl = Syllabifier()
        en_token = FakeWordToken("hello", start_ms=0, end_ms=600, language="en")
        ru_token = FakeWordToken("привет", start_ms=1000, end_ms=1600, language="ru")

        result = syl.syllabify([en_token, ru_token])

        # The Russian syllables must start at or after 1.0 s
        ru_syllables = result[2:]
        for timing in ru_syllables:
            assert timing.start >= 1.0 - 1e-9

    def test_flat_list_is_returned_for_multiple_tokens(self) -> None:
        """syllabify() returns a flat list regardless of how many tokens there are."""
        syl = Syllabifier()
        tokens = [
            FakeWordToken("cat", start_ms=0, end_ms=300, language="en"),
            FakeWordToken("dog", start_ms=400, end_ms=700, language="en"),
            FakeWordToken("bird", start_ms=800, end_ms=1100, language="en"),
        ]

        result = syl.syllabify(tokens)

        assert isinstance(result, list)
        assert all(isinstance(t, SyllableTiming) for t in result)
        assert len(result) == 3  # all three are single-syllable words


# ---------------------------------------------------------------------------
# SyllableTiming return type
# ---------------------------------------------------------------------------


class TestReturnType:
    """syllabify() must always return SyllableTiming instances."""

    def test_returns_list_of_syllable_timings(self) -> None:
        syl = Syllabifier()
        token = FakeWordToken("beautiful", start_ms=0, end_ms=1000, language="en")

        result = syl.syllabify([token])

        assert all(isinstance(item, SyllableTiming) for item in result)

    def test_syllable_timing_fields_are_present(self) -> None:
        syl = Syllabifier()
        token = FakeWordToken("hello", start_ms=0, end_ms=600, language="en")

        result = syl.syllabify([token])

        for item in result:
            assert hasattr(item, "syllable")
            assert hasattr(item, "start")
            assert hasattr(item, "end")
            assert isinstance(item.syllable, str)
            assert isinstance(item.start, float)
            assert isinstance(item.end, float)

    def test_start_less_than_end_for_all_syllables(self) -> None:
        syl = Syllabifier()
        token = FakeWordToken("beautiful", start_ms=0, end_ms=900, language="en")

        result = syl.syllabify([token])

        for item in result:
            assert item.start < item.end, (
                f"Syllable '{item.syllable}' has start >= end"
            )


# ---------------------------------------------------------------------------
# Whitespace-only and stripped tokens
# ---------------------------------------------------------------------------


class TestWhitespaceTokens:
    """Tokens containing only whitespace are skipped (stripped text is empty)."""

    def test_whitespace_only_token_is_skipped(self) -> None:
        syl = Syllabifier()
        tokens = [
            FakeWordToken("  ", start_ms=0, end_ms=100, language="en"),
        ]

        result = syl.syllabify(tokens)

        assert result == []

    def test_whitespace_token_between_real_tokens_is_skipped(self) -> None:
        syl = Syllabifier()
        tokens = [
            FakeWordToken("cat", start_ms=0, end_ms=300, language="en"),
            FakeWordToken("  ", start_ms=300, end_ms=400, language="en"),
            FakeWordToken("dog", start_ms=400, end_ms=700, language="en"),
        ]

        result = syl.syllabify(tokens)

        assert len(result) == 2
        assert result[0].syllable == "cat"
        # Second word gets space prefix for display.
        assert result[1].syllable.strip() == "dog"
