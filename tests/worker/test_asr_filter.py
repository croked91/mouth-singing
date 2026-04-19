"""Unit tests for the ASR-driven lyrics junk filter."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from worker.common.lyrics.matching.asr_filter import (
    ASRLyricsFilter,
    _parse_llm_grey_response,
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _filter(**kw) -> ASRLyricsFilter:
    """Build a filter with LLM disabled unless explicitly enabled."""
    kw.setdefault("use_llm_grey", False)
    kw.setdefault("deepseek_api_key", None)
    return ASRLyricsFilter(**kw)


# ----------------------------------------------------------------------
# Tokenization & coverage basics
# ----------------------------------------------------------------------


class TestCoverageBasics:
    async def test_identical_asr_and_candidate_keeps_everything(self):
        f = _filter()
        text = "тёплое место но улицы ждут\nотпечатков наших ног"
        result = await f.filter(text, text, "ru")
        assert "тёплое место но улицы ждут" in result.lyrics_clean
        assert "отпечатков наших ног" in result.lyrics_clean
        assert result.lines_dropped == 0
        assert result.lines_trimmed == 0

    async def test_completely_unrelated_triggers_safety_bypass(self):
        f = _filter()
        asr = "тёплое место но улицы ждут отпечатков наших ног"
        cand = (
            "группа крови на рукаве мой порядковый номер\n"
            "пожелай мне удачи в бою пожелай мне\n"
            "не остаться в этой траве"
        )
        result = await f.filter(asr, cand, "ru")
        assert result.safety_bypass is True
        # Original is returned untouched.
        assert result.lyrics_clean == cand

    async def test_empty_asr_returns_original(self):
        f = _filter()
        cand = "some lyrics here"
        result = await f.filter("", cand, "en")
        assert result.lyrics_clean == cand

    async def test_empty_candidate_returns_empty(self):
        f = _filter()
        result = await f.filter("some asr text", "", "ru")
        assert result.lyrics_clean == ""


# ----------------------------------------------------------------------
# Whole-line drops
# ----------------------------------------------------------------------


class TestWholeLineDrop:
    async def test_drops_pure_junk_metadata_line(self):
        f = _filter()
        asr = (
            "звёзды в небе светят ярко\n"
            "я иду по пустым улицам\n"
            "ночь накрыла город тенью"
        )
        cand = (
            "Дата: Сентября года текст композиции\n"
            "Звёзды в небе светят ярко\n"
            "Я иду по пустым улицам\n"
            "Ночь накрыла город тенью"
        )
        result = await f.filter(asr, cand, "ru")
        assert "Дата" not in result.lyrics_clean
        assert "Звёзды в небе светят ярко" in result.lyrics_clean
        assert "Я иду по пустым улицам" in result.lyrics_clean
        assert result.lines_dropped >= 1

    async def test_drops_backing_vocal_not_in_asr(self):
        # Correctness: Whisper and CTC share the vocals stem after back-vocal
        # separation, so if Whisper didn't hear a line, CTC won't align it.
        f = _filter()
        asr = (
            "я помню тот летний вечер\n"
            "мы сидели у реки\n"
            "солнце тонуло в волнах"
        )
        cand = (
            "я помню тот летний вечер\n"
            "мы сидели у реки\n"
            "совсем чужие слова которых не было\n"
            "солнце тонуло в волнах"
        )
        result = await f.filter(asr, cand, "ru")
        assert "совсем чужие слова" not in result.lyrics_clean
        assert result.lines_dropped >= 1


# ----------------------------------------------------------------------
# Inline prefix / suffix trimming
# ----------------------------------------------------------------------


class TestInlineTrim:
    async def test_trims_pripev_prefix(self):
        f = _filter()
        asr = "мы будем танцевать всю ночь до самого утра"
        cand = "Припев 2: Мы будем танцевать всю ночь до самого утра"
        result = await f.filter(asr, cand, "ru")
        assert "Припев" not in result.lyrics_clean
        assert "Мы будем танцевать всю ночь до самого утра" in result.lyrics_clean
        assert result.lines_trimmed == 1

    async def test_trims_tekst_prefix(self):
        f = _filter()
        asr = "и ветер дует в лицо мне дорога зовёт меня вдаль"
        cand = "Текст: И ветер дует в лицо мне\nДорога зовёт меня вдаль"
        result = await f.filter(asr, cand, "ru")
        assert "Текст:" not in result.lyrics_clean
        assert "И ветер дует в лицо мне" in result.lyrics_clean
        assert "Дорога зовёт меня вдаль" in result.lyrics_clean

    async def test_trims_radio_edit_prefix(self):
        f = _filter()
        asr = "я помню тот вечер когда мы встретились впервые"
        cand = "(Radio Edit) Я помню тот вечер когда мы встретились впервые"
        result = await f.filter(asr, cand, "en")
        assert "radio" not in result.lyrics_clean.lower()
        assert "edit" not in result.lyrics_clean.lower()
        assert "Я помню тот вечер" in result.lyrics_clean

    async def test_does_not_trim_legitimate_first_word(self):
        """If the first word is non-marker and not suspicious, keep it even
        if Whisper missed it — avoid false positives on the real verse."""
        f = _filter()
        # "Навсегда" — real word, Whisper missed it (dropped first word),
        # but it's not a marker. Don't trim.
        asr = "остаёмся вместе ты и я на этой земле"
        cand = "Навсегда остаёмся вместе ты и я на этой земле"
        result = await f.filter(asr, cand, "ru")
        # Line is kept as-is — no trim, no drop.
        assert "Навсегда" in result.lyrics_clean
        assert result.lines_trimmed == 0


# ----------------------------------------------------------------------
# User-facing real example
# ----------------------------------------------------------------------


class TestDzettaKometa:
    async def test_dzetta_kometa_long_junk_prefix_goes_grey_without_llm_kept(self):
        """Line 'Dzetta Кометы (Radio Mix) Дата: Сентрября г, I: Звёзды в
        небе светят ярко' has ~8 junk words before real content — too long
        for inline trim (max_prefix=6). Lands in grey zone. Without LLM it
        stays; with LLM it should be trimmed/dropped."""
        f = _filter()
        asr = (
            "звёзды в небе светят ярко я смотрю на них с земли "
            "холодный ветер задевает щёки тихая ночь вокруг меня"
        )
        cand = (
            "Dzetta Кометы (Radio Mix) Дата: Сентрября г, I: "
            "Звёзды в небе светят ярко\n"
            "Я смотрю на них с земли\n"
            "Холодный ветер задевает щёки\n"
            "Тихая ночь вокруг меня"
        )
        result = await f.filter(asr, cand, "ru")
        # Other lines are kept verbatim.
        assert "Я смотрю на них с земли" in result.lyrics_clean
        assert "Холодный ветер задевает щёки" in result.lyrics_clean
        # Without LLM, grey line is kept (fail-safe).
        assert result.llm_called is False
        assert result.grey_zone_lines >= 1


# ----------------------------------------------------------------------
# Song-hook protection (don't collapse repeats, don't drop valid hooks)
# ----------------------------------------------------------------------


class TestHookPreservation:
    async def test_keeps_hook_line_when_hook_is_in_asr(self):
        f = _filter()
        asr = "ла ла ла ла ла ла ла ла ла"
        cand = "Ла ла ла ла ла ла\nЛа ла ла"
        result = await f.filter(asr, cand, "ru")
        assert "Ла ла ла" in result.lyrics_clean
        assert result.lines_dropped == 0

    async def test_does_not_collapse_consecutive_repeats(self):
        f = _filter()
        asr = "белые розы белые розы беззащитны"
        cand = "Белые розы белые розы беззащитны"
        result = await f.filter(asr, cand, "ru")
        assert result.lyrics_clean.lower().count("белые розы") == 2


# ----------------------------------------------------------------------
# Short lines → grey → keep without LLM
# ----------------------------------------------------------------------


class TestGreyZone:
    async def test_short_line_goes_grey_and_kept_without_llm(self):
        f = _filter()
        asr = "я тебя люблю и буду любить всегда наверное до самой смерти"
        # "О да!" is a 2-word line → grey; Whisper won't transcribe it well.
        cand = (
            "Я тебя люблю\n"
            "О да!\n"
            "И буду любить всегда"
        )
        result = await f.filter(asr, cand, "ru")
        assert "О да!" in result.lyrics_clean
        assert result.grey_zone_lines >= 1

    async def test_llm_grey_decision_drop(self):
        f = _filter(use_llm_grey=True, deepseek_api_key="fake")
        asr = "я тебя люблю и буду любить всегда"
        cand = (
            "Я тебя люблю\n"
            "ПРИПЕВ II\n"
            "И буду любить всегда"
        )
        # Mock LLM: decide ПРИПЕВ II → drop.
        async def fake_llm(asr_text, grey_lines, language):
            return {i: ("drop", None) for i, _ in grey_lines}

        with patch.object(
            f, "_llm_decide_grey",
            new=AsyncMock(side_effect=fake_llm),
        ):
            result = await f.filter(asr, cand, "ru")
        assert "ПРИПЕВ" not in result.lyrics_clean
        assert "Я тебя люблю" in result.lyrics_clean
        assert "И буду любить всегда" in result.lyrics_clean
        assert result.llm_called is True


# ----------------------------------------------------------------------
# Russian morphology sanity: lemma match should work
# ----------------------------------------------------------------------


class TestMorphology:
    async def test_whisper_lemma_variants_still_match(self):
        f = _filter()
        # Whisper heard inflected forms; candidate has normal form.
        asr = "любви твоей я ждал годами ночами звёзды я считал"
        cand = "Любовь твоя была моей ночами звёзды я считал"
        result = await f.filter(asr, cand, "ru")
        # "ночами звёзды я считал" fully present → line kept.
        assert "ночами звёзды" in result.lyrics_clean.lower()


# ----------------------------------------------------------------------
# LLM response parser
# ----------------------------------------------------------------------


class TestLLMResponseParser:
    def test_parses_raw_array(self):
        raw = '[{"id": 0, "action": "drop"}, {"id": 1, "action": "trim", "text": "hello"}]'
        out = _parse_llm_grey_response(raw, {0, 1})
        assert out == {0: ("drop", None), 1: ("trim", "hello")}

    def test_parses_wrapped_object(self):
        raw = '{"lines": [{"id": 0, "action": "keep"}]}'
        out = _parse_llm_grey_response(raw, {0})
        assert out == {0: ("keep", None)}

    def test_ignores_unknown_ids(self):
        raw = '[{"id": 99, "action": "drop"}]'
        out = _parse_llm_grey_response(raw, {0})
        assert out == {}

    def test_malformed_trim_becomes_keep(self):
        raw = '[{"id": 0, "action": "trim"}]'  # no "text" field
        out = _parse_llm_grey_response(raw, {0})
        assert out == {0: ("keep", None)}

    def test_invalid_json_returns_none(self):
        assert _parse_llm_grey_response("not json", {0}) is None

    def test_empty_string_returns_none(self):
        assert _parse_llm_grey_response("", {0}) is None


# ----------------------------------------------------------------------
# Positional (sandwich) protection
# ----------------------------------------------------------------------


class TestSandwichProtection:
    async def test_drop_sandwiched_between_keeps_is_rescued(self):
        """Regression: the 'Слава КПСС — Любимые песни настоящих людей'
        line was dropped by the LLM because its coverage was low (Whisper
        misses stylized spoken inserts) and its shape looks metadata-like.
        Because it's sandwiched between two lines that the filter keeps,
        it must be kept too — metadata doesn't sit mid-verse."""
        f = _filter(use_llm_grey=True, deepseek_api_key="fake")
        asr = (
            "я владимир путин значит что ебал власть в рот "
            "новые песни пишут те у кого старые плохие"
        )
        cand = (
            "Я Владимир Путин значит что?\n"
            "Ебал власть в рот\n"
            "Слава КПСС — Любимые песни настоящих людей\n"
            "Новые песни пишут те у кого старые плохие"
        )

        # Mock LLM to drop the suspicious-looking line (as happens in prod).
        async def fake_llm(asr_text, grey_lines, language):
            return {i: ("drop", None) for i, _ in grey_lines}

        with patch.object(
            f, "_llm_decide_grey", new=AsyncMock(side_effect=fake_llm),
        ):
            result = await f.filter(asr, cand, "ru")
        assert "Слава КПСС — Любимые песни настоящих людей" in result.lyrics_clean
        assert result.sandwich_rescued >= 1

    async def test_drop_sandwiched_through_blank_line(self):
        """Empty lines (paragraph breaks) don't break the sandwich."""
        f = _filter(use_llm_grey=True, deepseek_api_key="fake")
        asr = "мы сидели у реки солнце тонуло в волнах"
        # Short line → grey → mocked LLM decides drop → sandwich rescues it.
        cand = (
            "Мы сидели у реки\n"
            "\n"
            "Ах да\n"
            "\n"
            "Солнце тонуло в волнах"
        )

        async def fake_llm(asr_text, grey_lines, language):
            return {i: ("drop", None) for i, _ in grey_lines}

        with patch.object(
            f, "_llm_decide_grey", new=AsyncMock(side_effect=fake_llm),
        ):
            result = await f.filter(asr, cand, "ru")
        assert "Ах да" in result.lyrics_clean
        assert result.sandwich_rescued >= 1

    async def test_low_coverage_drop_not_rescued_by_sandwich(self):
        """A line whose words just aren't in the ASR (coverage=0) must NOT
        be rescued by sandwich protection — CTC can't align what Whisper
        didn't hear, so keeping it only wrecks neighbour timings."""
        f = _filter()
        asr = "мы сидели у реки солнце тонуло в волнах птицы пели тихо"
        cand = (
            "Мы сидели у реки\n"
            "Неизвестные слова которые Whisper не слышит вообще\n"
            "Солнце тонуло в волнах\n"
            "Птицы пели тихо"
        )
        result = await f.filter(asr, cand, "ru")
        assert "Неизвестные слова" not in result.lyrics_clean
        assert result.sandwich_rescued == 0

    async def test_trailing_drop_not_protected(self):
        """Drop at the end of the document has no right neighbour → no
        protection. Metadata / credits typically live at the edges."""
        f = _filter()
        asr = "мы сидели у реки солнце тонуло в волнах"
        cand = (
            "Мы сидели у реки\n"
            "Солнце тонуло в волнах\n"
            "Дата записи: Сентябрь 2023"
        )
        result = await f.filter(asr, cand, "ru")
        assert "Дата записи" not in result.lyrics_clean

    async def test_leading_drop_not_protected(self):
        """Drop at the start of the document has no left neighbour →
        metadata like a title/credit header still gets removed."""
        f = _filter()
        asr = "мы сидели у реки солнце тонуло в волнах"
        cand = (
            "Дата записи: Сентябрь 2023\n"
            "Мы сидели у реки\n"
            "Солнце тонуло в волнах"
        )
        result = await f.filter(asr, cand, "ru")
        assert "Дата записи" not in result.lyrics_clean

    async def test_two_consecutive_drops_stay_dropped(self):
        """A metadata block (two adjacent non-sung lines) doesn't qualify
        as a sandwich — neither line has both-side KEEP neighbours."""
        f = _filter()
        asr = "мы сидели у реки солнце тонуло в волнах птицы пели над водой"
        cand = (
            "Мы сидели у реки\n"
            "Дата записи: Сентябрь 2023\n"
            "Автор текста Иванов Петров\n"
            "Солнце тонуло в волнах\n"
            "Птицы пели над водой"
        )
        result = await f.filter(asr, cand, "ru")
        assert "Дата записи" not in result.lyrics_clean
        assert "Автор текста" not in result.lyrics_clean


# ----------------------------------------------------------------------
# Structural preservation
# ----------------------------------------------------------------------


class TestStructuralPreservation:
    async def test_paragraph_breaks_preserved(self):
        f = _filter()
        asr = (
            "звёзды в небе светят ярко\n"
            "я иду по пустым улицам\n"
            "ночь накрыла город тенью"
        )
        cand = (
            "Звёзды в небе светят ярко\n\n"
            "Я иду по пустым улицам\n\n"
            "Ночь накрыла город тенью"
        )
        result = await f.filter(asr, cand, "ru")
        # Double newlines (paragraph) preserved.
        assert "\n\n" in result.lyrics_clean

    async def test_clean_candidate_roundtrip(self):
        f = _filter()
        text = (
            "Звёзды в небе светят ярко\n"
            "Я иду по пустым улицам\n"
            "Ночь накрыла город тенью"
        )
        result = await f.filter(text, text, "ru")
        # No drops, no trims — output should match input.
        assert result.lines_dropped == 0
        assert result.lines_trimmed == 0
        for line in text.splitlines():
            assert line in result.lyrics_clean
