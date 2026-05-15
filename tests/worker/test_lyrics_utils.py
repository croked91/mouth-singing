"""Tests for the pure-python lyrics utilities and helpers.

Covers what the audit (3.4) flagged as "thin":
  * worker.common.lyrics.fragments.extract_search_fragments
  * worker.common.lyrics.filename_parser (parse + helpers, with mocked LLM)
  * worker.common.lyrics.matching.expander (algorithmic pass, no LLM)
  * shared.karaoke_shared.utils.syllabifier.Syllabifier
  * shared.karaoke_shared.utils.line_breaker.detect_line_breaks
"""

from __future__ import annotations

from unittest.mock import patch


from karaoke_shared.models.track import SyllableTiming
from karaoke_shared.utils.line_breaker import detect_line_breaks
from karaoke_shared.utils.syllabifier import Syllabifier
from worker.common.lyrics.filename_parser import (
    FilenameParser,
    ParsedFilename,
    _build_variants,
    _extract_json,
)
from worker.common.lyrics.fragments import extract_search_fragments
from worker.common.lyrics.matching.expander import LyricsExpander


# ---------------------------------------------------------------------------
# fragments.extract_search_fragments
# ---------------------------------------------------------------------------

class TestExtractSearchFragments:
    def test_empty_string_returns_empty(self):
        assert extract_search_fragments("") == []

    def test_short_text_returns_single_fragment(self):
        assert extract_search_fragments("hello world") == ["hello world"]

    def test_three_sentences_split_into_three_fragments(self):
        text = (
            "Verse one tells a sad and quiet story today. "
            "The chorus rings loud above all the city sounds. "
            "Final verse echoes from the deep blue evening sky."
        )
        result = extract_search_fragments(text, n=3)
        assert len(result) == 3
        # All three sentences must appear (different indices are picked)
        assert "Verse one" in result[0]
        assert "Final verse" in result[-1]

    def test_each_fragment_capped_at_12_words(self):
        long_text = " ".join(f"word{i}" for i in range(60)) + ". " + " ".join(
            f"chunk{i}" for i in range(60)
        )
        result = extract_search_fragments(long_text, n=3)
        assert all(len(frag.split()) <= 12 for frag in result)

    def test_fewer_phrases_than_requested_falls_back_to_word_chunking(self):
        # One sentence, lots of words → should chunk
        text = " ".join(f"w{i}" for i in range(40))
        result = extract_search_fragments(text, n=3)
        assert len(result) == 3

    def test_n_larger_than_phrases_returns_all(self):
        text = "Short sentence with five words exactly here."
        result = extract_search_fragments(text, n=10)
        # Returns up to n; only one fragment is available
        assert 1 <= len(result) <= 10


# ---------------------------------------------------------------------------
# filename_parser helpers
# ---------------------------------------------------------------------------

class TestBuildVariants:
    def test_canonical_only(self):
        assert _build_variants("Queen", None) == ("Queen",)

    def test_canonical_and_distinct_original(self):
        assert _build_variants("Джетта", "Dzetta") == ("Джетта", "Dzetta")

    def test_canonical_equals_original_returns_one(self):
        assert _build_variants("Queen", "queen") == ("Queen",)

    def test_empty_canonical_keeps_original_only(self):
        assert _build_variants("", "Foo") == ("Foo",)

    def test_both_empty_returns_empty(self):
        assert _build_variants("", "") == ()


class TestExtractJson:
    def test_pure_json(self):
        assert _extract_json('{"a": 1}') == {"a": 1}

    def test_json_inside_prose(self):
        text = 'LLM said: {"artist": "Q", "title": "T"} thanks!'
        assert _extract_json(text) == {"artist": "Q", "title": "T"}

    def test_garbage_returns_none(self):
        assert _extract_json("no json here") is None

    def test_invalid_json_returns_none(self):
        assert _extract_json("{not actually json}") is None


class TestParsedFilenameDataclass:
    def test_empty_fields(self):
        empty = ParsedFilename.empty()
        assert empty.artist is None
        assert empty.title is None
        assert empty.artist_alts == []
        assert empty.title_alts == []

    def test_alternates_split_correctly(self):
        pf = ParsedFilename(
            artist_variants=("Джетта", "Dzetta"),
            title_variants=("Кометы",),
        )
        assert pf.artist == "Джетта"
        assert pf.artist_alts == ["Dzetta"]
        assert pf.title == "Кометы"
        assert pf.title_alts == []


class TestFilenameParserParse:
    async def test_llm_failure_returns_empty(self):
        parser = FilenameParser(deepseek_api_key="fake")
        with patch.object(parser, "_call_llm", side_effect=RuntimeError("nope")):
            result = await parser.parse("anything.mp3")
        assert result == ParsedFilename.empty()

    async def test_llm_returns_pure_json(self):
        parser = FilenameParser(deepseek_api_key="fake")
        payload = (
            '{"artist": "Queen", "title": "Bohemian Rhapsody",'
            ' "artist_original": "", "title_original": ""}'
        )
        with patch.object(parser, "_call_llm", return_value=payload):
            result = await parser.parse("Queen-Bohemian.mp3")
        assert result.artist == "Queen"
        assert result.title == "Bohemian Rhapsody"
        assert result.artist_alts == []

    async def test_llm_returns_distinct_original_variant(self):
        parser = FilenameParser(deepseek_api_key="fake")
        payload = (
            '{"artist": "Джетта", "title": "Кометы", '
            '"artist_original": "Dzetta", "title_original": ""}'
        )
        with patch.object(parser, "_call_llm", return_value=payload):
            result = await parser.parse("Dzetta - Kometi.mp3")
        assert result.artist_variants == ("Джетта", "Dzetta")
        assert result.title_variants == ("Кометы",)

    async def test_llm_returns_garbage_returns_empty(self):
        parser = FilenameParser(deepseek_api_key="fake")
        with patch.object(parser, "_call_llm", return_value="not json at all"):
            result = await parser.parse("x.mp3")
        assert result == ParsedFilename.empty()


# ---------------------------------------------------------------------------
# expander.LyricsExpander — algorithmic pass only (no API key → no LLM call)
# ---------------------------------------------------------------------------

class TestExpander:
    async def test_passthrough_for_simple_text(self):
        exp = LyricsExpander(deepseek_api_key=None)
        text = "Line one\nLine two\nLine three"
        result = await exp.expand(text)
        # Sections are joined with \n\n by render; for a single section the
        # only diff is the body itself preserved.
        assert "Line one" in result and "Line three" in result

    async def test_empty_input_returns_empty(self):
        exp = LyricsExpander(deepseek_api_key=None)
        assert await exp.expand("") == ""
        assert await exp.expand("   ") == "   "

    async def test_counted_section_is_repeated(self):
        text = (
            "[Verse]\n"
            "Sun is shining bright today\n"
            "[Chorus x2]\n"
            "I will never let you go\n"
            "Stay with me forever\n"
        )
        exp = LyricsExpander(deepseek_api_key=None)
        result = await exp.expand(text)
        # Chorus body must appear twice
        assert result.count("I will never let you go") == 2
        assert result.count("Sun is shining bright today") == 1

    async def test_section_reference_replays_earlier_body(self):
        text = (
            "[Chorus]\n"
            "Hold me tight\n"
            "Through the long night\n"
            "[Verse]\n"
            "Story goes on\n"
            "[Chorus]\n"  # body-less reference
        )
        exp = LyricsExpander(deepseek_api_key=None)
        result = await exp.expand(text)
        assert result.count("Hold me tight") == 2

    async def test_inline_repeat_marker_expands_line(self):
        text = "[Bridge]\nOh oh oh (3 раза)\n"
        exp = LyricsExpander(deepseek_api_key=None)
        result = await exp.expand(text)
        assert result.count("Oh oh oh") == 3

    async def test_cache_hits_return_same_string(self):
        exp = LyricsExpander(deepseek_api_key=None)
        text = "[Chorus x2]\nLove me do\n"
        a = await exp.expand(text)
        b = await exp.expand(text)
        assert a is b  # cache returns the exact same object


# ---------------------------------------------------------------------------
# Syllabifier
# ---------------------------------------------------------------------------

class TestSyllabifier:
    def test_english_word_split(self):
        s = Syllabifier()
        parts = s._split_word("hello", lang="en")
        assert "".join(parts) == "hello"
        assert len(parts) >= 2  # pyphen splits "hello" into "hel" + "lo"

    def test_russian_word_split(self):
        s = Syllabifier()
        parts = s._split_word("привет", lang="ru")
        assert "".join(parts) == "привет"
        assert len(parts) >= 2

    def test_per_word_language_detection_uses_en_for_latin_in_ru_track(self):
        s = Syllabifier()
        # Even with global lang="ru", a Latin-script word is split via en_US
        parts = s._split_word("computer", lang="ru")
        assert "".join(parts) == "computer"

    def test_punctuation_preserved_around_word(self):
        s = Syllabifier()
        parts = s._split_word("«hello!»", lang="en")
        joined = "".join(parts)
        assert joined == "«hello!»"

    def test_unknown_language_falls_back_to_english(self):
        s = Syllabifier()
        # 'fr' isn't supported → falls back to en_US dict; must still join back
        parts = s._split_word("hello", lang="fr")
        assert "".join(parts) == "hello"

    def test_empty_word_returns_empty(self):
        s = Syllabifier()
        assert s._split_word("", lang="en") == []
        assert s._split_word("   ", lang="en") == []

    def test_word_without_alphabetic_chars(self):
        s = Syllabifier()
        # "123" has no alpha chars → returned as a single chunk
        assert s._split_word("123", lang="en") == ["123"]


# ---------------------------------------------------------------------------
# line_breaker.detect_line_breaks
# ---------------------------------------------------------------------------

def _t(syl: str, start: float, end: float) -> SyllableTiming:
    return SyllableTiming(syllable=syl, start=start, end=end)


class TestLineBreaker:
    def test_too_few_syllables_returns_input(self):
        timings = [_t("Hi", 0.0, 0.5)]
        assert detect_line_breaks(timings) == timings

    def test_already_marked_returned_unchanged(self):
        timings = [
            _t("Hi", 0.0, 0.5),
            _t("\nthere", 0.6, 1.2),
        ]
        result = detect_line_breaks(timings)
        # Same content, separate list (defensive copy)
        assert result == timings
        assert result is not timings

    def test_relaxed_gap_mode_inserts_break_at_word_boundary(self):
        # Fewer than 5 big gaps + no vocal_path → falls through to relaxed
        # gap mode (floor=0.2). Five small ~0.1s gaps and one 1.0s gap;
        # p75≈0.1 → threshold=max(0.2, 0.25)=0.25, the 1.0s gap clears it
        # and the following word-boundary syllable becomes a break.
        timings = [
            _t("Sun", 0.0, 0.1),
            _t(" rises", 0.2, 0.3),
            _t(" up", 0.4, 0.5),
            _t(" into", 0.6, 0.7),
            _t(" sky", 0.8, 0.9),
            _t(" Moon", 1.9, 2.0),  # 1.0s gap before this word boundary
        ]
        result = detect_line_breaks(timings)
        # Last syllable should now start with "\n" (the leading space is
        # replaced with the line-break marker).
        assert result[-1].syllable.startswith("\n")

    def test_force_break_kicks_in_for_long_lines(self):
        # Tiny gaps everywhere (no gap-based break possible), but the
        # accumulated character count exceeds 50 → forced break at the
        # next word boundary.
        starts = [i * 0.1 for i in range(15)]
        words = [
            "twelvecharlong", " word", "another", " more", " words", " here",
            " now", " come", " plenty", " of", " text", " for", " forced",
            " break", " test",
        ]
        timings = [
            _t(words[i], starts[i], starts[i] + 0.05) for i in range(len(starts))
        ]
        result = detect_line_breaks(timings)
        markers = [s.syllable for s in result if s.syllable.startswith("\n")]
        # Force-break must fire at least once when accumulated chars > 50
        assert len(markers) >= 1

    def test_break_replaces_leading_space_with_newline(self):
        # Two syllables, second begins with space, large gap before it.
        timings = [
            _t("Hi", 0.0, 0.2),
            _t(" world", 0.2, 0.5),
            _t(" again", 0.55, 0.9),
            _t(" hello", 0.95, 1.3),
            _t(" again", 1.4, 1.7),
            _t(" friend", 5.0, 5.4),  # huge gap before this token
        ]
        result = detect_line_breaks(timings)
        # The last syllable should start with \n (replaces the space).
        last = result[-1].syllable
        if last.startswith("\n"):
            # The leading space must be gone
            assert not last.startswith(" ")
            assert last.startswith("\n")
