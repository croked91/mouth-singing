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

    def test_rare_anchor_is_length_neutral(self):
        """Regression: a longer candidate must not dominate rare_anchor_score
        simply by containing more 5-grams. Previously a 'Rework' version
        twice the length of the correct one could take rare_anchor=1.0 just
        because it had more matchable anchors, overriding a more accurate
        short version. Fix normalizes by candidate length (density).
        """
        shared_prefix = (
            "тёплое место но улицы ждут отпечатков наших ног "
            "звёздная пыль на сапогах мягкое кресло над пропастью ждёт"
        )
        asr = normalize_text(shared_prefix, "ru")
        # Correct candidate == ASR.
        correct = normalize_text(shared_prefix, "ru")
        # Long remix: same matched content, plus ~20 extra unique verses
        # that are also matched against a LONGER ASR. But here ASR is the
        # short one, so extras don't match — they just dilute density.
        remix = normalize_text(
            shared_prefix + " "
            + "экстра строка ремикса с дополнительными словами которые "
            "никогда не появятся в оригинальной расшифровке песни потому "
            "что они добавлены только в этой версии и не имеют отношения "
            "к оригинальной записи вокалиста и инструментальному "
            "сопровождению которое использовалось при записи в студии",
            "ru",
        )
        feats = score_all(asr, [correct, remix])
        # Density-normalized: correct has all its 5-grams matched; remix
        # only matches the shared prefix portion → lower density.
        assert feats[0].rare_anchor_score >= feats[1].rare_anchor_score
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

    async def test_weak_win_invokes_tiebreaker_when_api_key_present(self):
        """Weak wins (composite ≥0.45 but <0.65) call the LLM tiebreaker on
        every runner-up. This catches cases like Slava KPSS 'Культура G' vs
        'Культура G (Rework 2023)' where the Rework can edge out the
        original on rare_anchor but is actually the wrong version.
        """
        from worker.common.lyrics.matching.scorer import MatchFeatures

        matcher = LyricsMatcher(
            expander=LyricsExpander(deepseek_api_key=None),
            deepseek_api_key="fake-key",
        )
        asr = "some asr text that does not matter because scorer is mocked"
        original = self._make_cand(
            "Artist", "Песня", "lyrics text 1", "lrclib",
        )
        rework = self._make_cand(
            "Artist", "Песня (Rework)", "lyrics text 2", "genius",
        )

        # Force scores into the weak-win band (top ≥0.45, gap ≥0.10, both <0.65).
        fake_features = [
            MatchFeatures(0.5, 0.5, 0.5, 0.5, 0.5, 0.0, 0.0, 0.40),  # original
            MatchFeatures(0.5, 0.5, 0.5, 0.5, 0.5, 0.0, 0.0, 0.52),  # rework (top)
        ]
        with patch(
            "worker.common.lyrics.matching.matcher.score_all",
            return_value=fake_features,
        ), patch.object(
            matcher, "_tiebreak", return_value=None,
        ) as mock_tb:
            await matcher.match(asr, [original, rework], "ru")
        mock_tb.assert_called_once()

    async def test_weak_small_gap_not_rejected(self):
        """Two near-identical candidates in the weak range (gap < 2*margin)
        must not be rejected outright. Covers the case where SearxNG
        returns the same song twice with slightly different artist spelling
        — historically the matcher rejected both and fell back to raw ASR.
        """
        from worker.common.lyrics.matching.scorer import MatchFeatures

        matcher = LyricsMatcher(
            expander=LyricsExpander(deepseek_api_key=None),
            deepseek_api_key=None,  # no LLM — must still pick top
        )
        asr = "some asr text"
        a = self._make_cand("Artist One", "Song", "lyrics v1", "searxng")
        b = self._make_cand("Artist Two", "Song", "lyrics v2", "searxng")
        fake_features = [
            MatchFeatures(0.5, 0.5, 0.5, 0.5, 0.5, 0.0, 0.0, 0.54),
            MatchFeatures(0.5, 0.5, 0.5, 0.5, 0.5, 0.0, 0.0, 0.52),
        ]
        with patch(
            "worker.common.lyrics.matching.matcher.score_all",
            return_value=fake_features,
        ):
            result = await matcher.match(asr, [a, b], "ru")
        assert result is not None
        # Top is the first one (0.54 > 0.52) — the matcher must accept it.
        assert result.artist == "Artist One"

    async def test_weak_tiebreaker_returns_picked_candidate(self):
        """When the weak tiebreaker picks the runner-up, that's what we
        return — not the raw top."""
        from worker.common.lyrics.matching.matcher import _Ranked
        from worker.common.lyrics.matching.scorer import MatchFeatures

        matcher = LyricsMatcher(
            expander=LyricsExpander(deepseek_api_key=None),
            deepseek_api_key="fake-key",
        )
        asr = "some asr text"
        original = self._make_cand(
            "Artist", "Песня", "original lyrics", "lrclib",
        )
        rework = self._make_cand(
            "Artist", "Песня (Rework)", "rework lyrics", "genius",
        )

        fake_features = [
            MatchFeatures(0.5, 0.5, 0.5, 0.5, 0.5, 0.0, 0.0, 0.40),  # original
            MatchFeatures(0.5, 0.5, 0.5, 0.5, 0.5, 0.0, 0.0, 0.52),  # rework (top)
        ]

        async def pick_runner_up(
            asr_text, a: _Ranked, b: _Ranked, language,
            artist_hints=None, title_hints=None,
        ):
            # Return whichever candidate is NOT the Rework, regardless
            # of which side it landed on after ranking.
            return a if "Rework" not in a.candidate.title else b

        with patch(
            "worker.common.lyrics.matching.matcher.score_all",
            return_value=fake_features,
        ), patch.object(matcher, "_tiebreak", side_effect=pick_runner_up):
            result = await matcher.match(asr, [original, rework], "ru")
        assert result is not None
        assert "Rework" not in result.title


# ======================================================================
# matcher.py — filename-hint bonus
# ======================================================================


class TestHintBonus:
    """Bonus for candidate artist/title matching filename-derived hints.

    Reproduces the trololo-style failure: degenerate ASR (just la-la-la)
    causes an unrelated candidate to win on phonetic coverage. The hint
    bonus must override that when the filename clearly identifies the
    correct song.
    """

    def _make_cand(self, artist, title, lyrics, source="provider"):
        return LyricsCandidate(
            artist=artist, title=title, lyrics=lyrics, source=source,
        )

    def test_hint_score_matches_combined_artist_and_title(self):
        from worker.common.lyrics.matching.matcher import _hint_match_score

        # Genius-style: canonical artist lives inside the title field.
        score = _hint_match_score(
            cand_artist="Genius English Translations",
            cand_title="Эдуард Хиль (Eduard Khil) - Я очень рад (English)",
            artist_hints=["Эдуард Хиль", "Eduard Hil"],
            title_hints=["Я очень рад", "Ya ochen rad"],
        )
        assert score > 0.85

    def test_hint_score_zero_for_unrelated_candidate(self):
        from worker.common.lyrics.matching.matcher import _hint_match_score

        score = _hint_match_score(
            cand_artist="The Avalanches",
            cand_title="Two Hearts in 3/4 Time",
            artist_hints=["Эдуард Хиль", "Eduard Hil"],
            title_hints=["Я очень рад", "Ya ochen rad"],
        )
        assert score < 0.4

    def test_hint_score_zero_when_no_hints(self):
        from worker.common.lyrics.matching.matcher import _hint_match_score

        score = _hint_match_score(
            cand_artist="Whatever",
            cand_title="Whatever Else",
            artist_hints=[],
            title_hints=[],
        )
        assert score == 0.0

    async def test_hint_score_passed_to_scorer(self):
        """Matcher must compute hint scores per candidate and forward them
        to ``score_all``. This is the wiring contract — ASR-vs-hint balance
        is enforced by ``_W_HINT`` inside the scorer itself.
        """
        matcher = LyricsMatcher(
            expander=LyricsExpander(deepseek_api_key=None),
            deepseek_api_key=None,
        )
        wrong = self._make_cand(
            "The Avalanches", "Two Hearts in 3/4 Time", "la la la", "genius",
        )
        correct = self._make_cand(
            "Эдуард Хиль", "Я очень рад", "ла ла ла", "genius",
        )

        captured: dict = {}

        def fake_score_all(asr_norm, cand_norms, hint_scores=None):
            captured["hint_scores"] = hint_scores
            from worker.common.lyrics.matching.scorer import MatchFeatures
            # Whatever — return zero, we only care about the wiring here.
            return [MatchFeatures(0, 0, 0, 0, 0, 1.0, h, 0.0)
                    for h in (hint_scores or [0.0] * len(cand_norms))]

        with patch(
            "worker.common.lyrics.matching.matcher.score_all",
            side_effect=fake_score_all,
        ):
            await matcher.match(
                "la la la", [wrong, correct], "ru",
                artist_hints=["Эдуард Хиль", "Eduard Hil"],
                title_hints=["Я очень рад", "Ya ochen rad"],
            )

        scores = captured["hint_scores"]
        assert scores is not None
        assert len(scores) == 2
        assert scores[0] < 0.4, "unrelated candidate must score low"
        assert scores[1] > 0.85, "matching candidate must score high"

    async def test_tiebreak_prompt_includes_hints_and_candidate_metadata(self):
        """When the LLM tiebreaker fires on a close strong-band call, it
        receives the filename hint and each candidate's artist/title — the
        bare lyrics aren't enough when ASR is degenerate (la-la songs).
        """
        from worker.common.lyrics.matching.scorer import MatchFeatures

        matcher = LyricsMatcher(
            expander=LyricsExpander(deepseek_api_key=None),
            deepseek_api_key="fake-key",
            margin=0.05,
        )
        wrong = self._make_cand(
            "The Avalanches", "Two Hearts in 3/4 Time",
            "la la la doo da da", "genius",
        )
        correct = self._make_cand(
            "Эдуард Хиль", "Я очень рад",
            "ла ла ла", "genius",
        )

        # Land both candidates in strong-band with sub-margin gap.
        fake_features = [
            MatchFeatures(0.9, 0.9, 0.9, 0.5, 0.5, 0.0, 0.0, 0.71),
            MatchFeatures(0.9, 0.9, 0.9, 0.5, 0.5, 0.0, 1.0, 0.73),
        ]

        captured: dict = {}

        def fake_llm_call(
            asr_text, a, b, language, artist_hints, title_hints,
        ):
            captured["user_prompt"] = (
                f'<asr language="{language}">\n{asr_text}\n</asr>\n'
                f'a={a.candidate.artist}|{a.candidate.title} '
                f'b={b.candidate.artist}|{b.candidate.title} '
                f'hints_a={artist_hints} hints_t={title_hints}'
            )
            captured["artist_hints"] = list(artist_hints)
            captured["title_hints"] = list(title_hints)
            captured["a_meta"] = (a.candidate.artist, a.candidate.title)
            captured["b_meta"] = (b.candidate.artist, b.candidate.title)
            return "2"  # pick the runner-up

        with patch(
            "worker.common.lyrics.matching.matcher.score_all",
            return_value=fake_features,
        ), patch.object(matcher, "_call_llm_tiebreak", side_effect=fake_llm_call):
            result = await matcher.match(
                "la la la", [wrong, correct], "ru",
                artist_hints=["Эдуард Хиль", "Eduard Hil"],
                title_hints=["Я очень рад"],
            )

        assert captured["artist_hints"] == ["Эдуард Хиль", "Eduard Hil"]
        assert captured["title_hints"] == ["Я очень рад"]
        # Both candidates' artist/title must reach the LLM (so it can map
        # hints onto the right candidate).
        metas = {captured["a_meta"], captured["b_meta"]}
        assert ("The Avalanches", "Two Hearts in 3/4 Time") in metas
        assert ("Эдуард Хиль", "Я очень рад") in metas
        assert result is not None

    async def test_hint_flips_close_call(self):
        """When two candidates are close on ASR, the hint bonus decides.

        Reproduces the trololo failure mode at a level the real scorer can
        cover: the wrong-text English candidate scored ~0.43 on ASR alone
        while the right-text English candidate scored ~0.71. The 0.282 gap
        is wider than ``_W_HINT`` (0.30) but only just — what we want to
        verify here is that *when* the gap is bridgeable, the hint flips it.
        """
        from worker.common.lyrics.matching.scorer import MatchFeatures, _W_HINT

        matcher = LyricsMatcher(
            expander=LyricsExpander(deepseek_api_key=None),
            deepseek_api_key=None,
        )
        wrong = self._make_cand(
            "Wrong Artist", "Wrong Song", "doesn't matter", "genius",
        )
        correct = self._make_cand(
            "Эдуард Хиль", "Я очень рад", "doesn't matter either", "genius",
        )

        # Wrong wins on ASR alone (0.55 vs 0.40). With a full hint bonus
        # (+0.30) the correct one reaches 0.70 and overtakes wrong at 0.55.
        def fake_score_all(asr_norm, cand_norms, hint_scores=None):
            base = [0.55, 0.40]
            return [
                MatchFeatures(
                    0.5, 0.5, 0.5, 0.5, 0.5, 0.0, h,
                    min(1.0, max(0.0, b + _W_HINT * h)),
                )
                for b, h in zip(base, hint_scores or [0.0, 0.0])
            ]

        with patch(
            "worker.common.lyrics.matching.matcher.score_all",
            side_effect=fake_score_all,
        ):
            result = await matcher.match(
                "asr", [wrong, correct], "ru",
                artist_hints=["Эдуард Хиль"],
                title_hints=["Я очень рад"],
            )

        assert result is not None
        assert result.artist == "Эдуард Хиль"
