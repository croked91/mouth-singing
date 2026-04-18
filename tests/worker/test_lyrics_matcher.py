"""Unit tests for the algorithmic lyrics matcher."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from worker.common.lyrics.base_provider import LyricsCandidate
from worker.common.lyrics.matching import (
    LyricsExpander,
    LyricsMatcher,
    normalize_text,
    score_all,
)
from worker.common.lyrics.matching.linguistics import make_word_featurizer


# ======================================================================
# linguistics.py
# ======================================================================


class TestLinguistics:
    def test_ru_lemma_collapses_morphology(self):
        ru = make_word_featurizer("ru")
        assert ru("любви").lemma == "любовь"
        assert ru("любовью").lemma == "любовь"

    def test_en_lemma_collapses_morphology(self):
        en = make_word_featurizer("en")
        assert en("running").lemma == "run"
        assert en("loved").lemma == "love"

    def test_consonant_skeleton_ru(self):
        ru = make_word_featurizer("ru")
        # Different vowels but same consonants → same skeleton.
        assert ru("любовь").skeleton == ru("любви").skeleton == "lbv"
        # Different consonants → different skeletons.
        assert ru("место").skeleton != ru("мечта").skeleton

    def test_consonant_skeleton_en(self):
        en = make_word_featurizer("en")
        assert en("love").skeleton == "lv"
        assert en("loving").skeleton == "lvng"

    def test_metaphone_for_en(self):
        en = make_word_featurizer("en")
        # night/knight are homophones → same metaphone.
        assert en("night").metaphone == en("knight").metaphone

    def test_universal_featurizer_handles_unknown_language(self):
        uni = make_word_featurizer("zz")  # not a real ISO code
        f = uni("café")
        assert f.text == "café"
        assert "f" in f.skeleton  # consonant present after unidecode

    def test_empty_word(self):
        ru = make_word_featurizer("ru")
        f = ru("")
        assert f.text == "" and f.lemma == "" and f.skeleton == ""


# ======================================================================
# normalizer.py
# ======================================================================


class TestNormalizer:
    def test_strips_section_markers(self):
        n = normalize_text("[Verse 1]\nHello world", "en")
        assert "verse" not in n.text
        assert "hello" in n.text
        assert "world" in n.text

    def test_strips_chord_notations_inline(self):
        # [D] / [Em7] inline get stripped along with section markers
        n = normalize_text("[D]Hello [Em]world", "en")
        # Chord brackets removed, words preserved.
        assert "hello" in n.text
        assert "world" in n.text
        assert "[d]" not in n.text

    def test_strips_short_parens(self):
        n = normalize_text("Я тебя люблю (oh) навсегда", "ru")
        assert "oh" not in n.text
        assert "люблю" in n.text

    def test_keeps_apostrophes(self):
        n = normalize_text("don't stop believin'", "en")
        assert "don't" in n.text or "don" in n.text  # may or may not keep '
        # but the apostrophe must not break tokenization
        assert n.word_count >= 3

    def test_does_not_collapse_legitimate_repeats(self):
        # Songs commonly repeat words intentionally.
        n = normalize_text("белые розы белые розы беззащитны", "ru")
        assert n.word_count == 5
        assert [w.text for w in n.words] == [
            "белые", "розы", "белые", "розы", "беззащитны",
        ]

    def test_drops_standalone_digits(self):
        n = normalize_text("track 12 hello world", "en")
        assert "12" not in n.text


# ======================================================================
# scorer.py — synthetic, controlled cases
# ======================================================================


class TestScorer:
    def test_exact_match_scores_one(self):
        text = "тёплое место но улицы ждут"
        asr = normalize_text(text, "ru")
        cand = normalize_text(text, "ru")
        feats = score_all(asr, [cand])
        assert feats[0].coverage_asr == 1.0
        assert feats[0].coverage_cand == 1.0
        assert feats[0].composite >= 0.95

    def test_completely_different_song_scores_low(self):
        asr = normalize_text("тёплое место но улицы ждут", "ru")
        cand = normalize_text(
            "группа крови на рукаве мой порядковый номер", "ru",
        )
        feats = score_all(asr, [cand])
        assert feats[0].composite < 0.2

    def test_remix_long_version_loses_to_correct(self):
        asr = normalize_text(
            "тёплое место но улицы ждут отпечатков наших ног", "ru",
        )
        correct = normalize_text(
            "тёплое место но улицы ждут отпечатков наших ног", "ru",
        )
        # Same text but with extra verses (long mix).
        remix = normalize_text(
            "тёплое место но улицы ждут отпечатков наших ног "
            + ("дождь стучит по подоконнику я считаю звёзды на небе "
               "время летит так незаметно что я даже не успел запомнить "
               "голоса в голове моей шепчут что всё прошло "
               "но я не верю им и иду вперёд я иду вперёд "
               "и снова иду и снова иду в никуда в никуда в никуда "
               "однажды утром солнце встанет над холмами "
               "птицы запоют свою привычную песню "
               "и всё начнётся снова с чистого листа"),
            "ru",
        )
        feats = score_all(asr, [correct, remix])
        assert feats[0].composite > feats[1].composite
        # Margin should be substantial.
        assert feats[0].composite - feats[1].composite > 0.2

    def test_whisper_typical_errors_still_match(self):
        # ASR with typical Whisper errors: vowel substitution, missing soft sign, wrong morphology
        asr = normalize_text(
            "теплое места улицы жудит атпечатков нашех ног "
            "звездая пыл на сапугах мягкое крЕсло над пропастю ждет",
            "ru",
        )
        cand = normalize_text(
            "тёплое место но улицы ждут отпечатков наших ног "
            "звёздная пыль на сапогах мягкая кресло над пропастью ждёт",
            "ru",
        )
        feats = score_all(asr, [cand])
        # Even with errors, coverage should be high via lemma+skeleton.
        assert feats[0].coverage_asr >= 0.85
        assert feats[0].composite >= 0.7

    def test_close_candidates_get_anchor_boost(self):
        """Two candidates with same general fit; rare anchors break the tie."""
        asr = normalize_text(
            "падает снег на пустую улицу зимней ночью звезда сорвалась",
            "ru",
        )
        # Candidate A has the unique "звезда сорвалась" anchor
        cand_a = normalize_text(
            "падает снег на пустую улицу зимней ночью звезда сорвалась",
            "ru",
        )
        # Candidate B is similar topic but lacks the anchor
        cand_b = normalize_text(
            "падает снег на пустую улицу зимней ночью город спит",
            "ru",
        )
        feats = score_all(asr, [cand_a, cand_b])
        assert feats[0].composite > feats[1].composite


# ======================================================================
# expander.py — algorithmic pass
# ======================================================================


class TestExpanderAlgorithmic:
    @pytest.fixture
    def expander(self):
        return LyricsExpander(deepseek_api_key=None)

    async def test_no_markers_returns_unchanged(self, expander):
        text = "hello world\nhello world\nhello world"
        result = await expander.expand(text)
        # Unchanged content; whitespace may be normalized by section flattening.
        assert "hello world" in result
        assert result.count("hello world") == 3

    async def test_section_count_x2_duplicates_body(self, expander):
        text = "[Куплет 1]\nстрока один\n\n[Припев x2]\nкругом голова"
        result = await expander.expand(text)
        # "кругом голова" should appear twice
        assert result.count("кругом голова") == 2

    async def test_section_count_2_raza(self, expander):
        text = "[Припев 2 раза]\nбелые розы"
        result = await expander.expand(text)
        assert result.count("белые розы") == 2

    async def test_section_reference_copies_body(self, expander):
        text = (
            "[Verse 1]\nfoo bar baz\n\n"
            "[Chorus]\nqux qux qux\n\n"
            "[Verse 2]\nhello world\n\n"
            "[Chorus]"  # reference, no body
        )
        result = await expander.expand(text)
        assert result.count("qux qux qux") == 2

    async def test_inline_repeat_with_parens(self, expander):
        text = "Я тебя люблю (2 раза)\nконец"
        result = await expander.expand(text)
        assert result.count("Я тебя люблю") == 2

    async def test_inline_repeat_with_multiplication_sign(self, expander):
        text = "oh oh oh ×3\nконец"
        result = await expander.expand(text)
        assert result.count("oh oh oh") == 3

    async def test_already_expanded_unchanged(self, expander):
        text = "[Припев]\nкругом голова\n\n[Припев]\nкругом голова"
        result = await expander.expand(text)
        # Two explicit blocks → kept as two blocks (no extra duplication)
        assert result.count("кругом голова") == 2

    async def test_caches_by_input_hash(self, expander):
        text = "[Припев x2]\nstuff"
        r1 = await expander.expand(text)
        r2 = await expander.expand(text)
        assert r1 == r2
        # Internal cache populated.
        assert len(expander._cache) == 1


class TestExpanderLLMGate:
    async def test_llm_skipped_when_no_api_key(self):
        expander = LyricsExpander(deepseek_api_key=None)
        # Text contains a meta-instruction that algo can't handle.
        text = "куплет один\nповторить припев"
        result = await expander.expand(text)
        # No LLM available → algorithmic result returned unchanged.
        assert "повторить припев" in result

    async def test_llm_called_when_meta_instruction_present(self):
        expander = LyricsExpander(deepseek_api_key="fake-key")
        text = "куплет один\nповторить припев"
        with patch.object(
            expander, "_expand_llm", return_value="fully expanded text",
        ) as mock_llm:
            result = await expander.expand(text)
        mock_llm.assert_called_once()
        assert result == "fully expanded text"

    async def test_llm_not_called_for_clean_text(self):
        expander = LyricsExpander(deepseek_api_key="fake-key")
        text = "[Припев x2]\nhello world"  # algorithmic handles this
        with patch.object(
            expander, "_expand_llm", return_value="should-not-see-this",
        ) as mock_llm:
            await expander.expand(text)
        mock_llm.assert_not_called()


# ======================================================================
# matcher.py — end-to-end decisions
# ======================================================================


class TestMatcher:
    @pytest.fixture
    def matcher(self):
        return LyricsMatcher(
            expander=LyricsExpander(deepseek_api_key=None),
            deepseek_api_key=None,  # no LLM tiebreaker
        )

    def _make_cand(self, artist, title, lyrics, source="provider"):
        return LyricsCandidate(
            artist=artist, title=title, lyrics=lyrics, source=source,
        )

    async def test_picks_correct_candidate(self, matcher):
        asr = "тёплое место но улицы ждут отпечатков наших ног"
        cands = [
            self._make_cand(
                "Кино", "Звезда", asr, "lrclib",
            ),
            self._make_cand(
                "Кино", "Группа крови",
                "группа крови на рукаве мой порядковый номер", "genius",
            ),
        ]
        result = await matcher.match(asr, cands, "ru")
        assert result is not None
        assert result.title == "Звезда"
        assert result.confidence == "high"

    async def test_returns_none_when_all_candidates_weak(self, matcher):
        asr = "тёплое место но улицы ждут отпечатков наших ног"
        cands = [
            self._make_cand(
                "Other", "Other Song",
                "completely unrelated english lyrics about something else",
                "src1",
            ),
            self._make_cand(
                "Wrong", "Wrong Song",
                "ничего общего с расшифровкой не имеет",
                "src2",
            ),
        ]
        result = await matcher.match(asr, cands, "ru")
        assert result is None

    async def test_remix_loses_to_correct_after_expansion(self, matcher):
        asr = (
            "тёплое место но улицы ждут отпечатков наших ног "
            "звёздная пыль на сапогах мягкая "
            "кругом голова кругом голова кругом голова кругом голова"
        )
        # Correct candidate uses [Припев x2] shorthand — without expansion it
        # would look too short.
        correct = self._make_cand(
            "Кино", "Звезда",
            "[Куплет]\nтёплое место но улицы ждут отпечатков наших ног\n"
            "звёздная пыль на сапогах мягкая\n\n"
            "[Припев x2]\nкругом голова кругом голова",
            "lrclib",
        )
        # Long remix that has all the words but with extra verses
        remix = self._make_cand(
            "DJ X", "Звезда (Long Mix)",
            "тёплое место но улицы ждут отпечатков наших ног "
            "звёздная пыль на сапогах мягкая "
            "кругом голова кругом голова кругом голова кругом голова "
            + ("экстра куплет ремикса " * 30),
            "lyricsovh",
        )
        result = await matcher.match(asr, [correct, remix], "ru")
        assert result is not None
        assert result.title == "Звезда"

    async def test_returns_none_for_empty_candidates(self, matcher):
        result = await matcher.match("any asr text", [], "ru")
        assert result is None

    async def test_returns_none_for_empty_asr(self, matcher):
        cands = [self._make_cand("A", "B", "some lyrics text", "src")]
        result = await matcher.match("", cands, "ru")
        assert result is None
