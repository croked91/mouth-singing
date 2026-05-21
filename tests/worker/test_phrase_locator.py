from __future__ import annotations

from worker.common.phrase_locator import AsrWord, PhraseLocator


def _locator() -> PhraseLocator:
    return PhraseLocator(
        storage=object(),  # locate() does not touch storage.
        track_id="track",
        audio_key="review-vocals/test.mp3",
        device="cpu",
    )


def test_english_contraction_variants_include_expanded_form() -> None:
    locator = _locator()

    variants = locator._query_variants("I don't read the newspapers", "en")

    assert "i don't read the newspapers" in variants
    assert "i do not read the newspapers" in variants
    assert "i dont read the newspapers" in variants


def test_locate_prefers_phonetic_match_when_asr_has_similar_word_error(monkeypatch) -> None:
    locator = _locator()

    def fake_phonemize(self, text: str, language: str) -> str:
        normalized = self._normalize_text(text, language)
        return (
            normalized
            .replace("don't", "do not")
            .replace("dont", "do not")
            .replace("need", "reed")
            .replace("read", "reed")
        )

    monkeypatch.setattr(PhraseLocator, "_phonemize_text", fake_phonemize)
    words = [
        AsrWord("Negotiations", 0.0, 0.5),
        AsrWord("breaking", 0.5, 0.9),
        AsrWord("down", 0.9, 1.2),
        AsrWord("I", 2.0, 2.1),
        AsrWord("don't", 2.1, 2.3),
        AsrWord("need", 2.3, 2.55),
        AsrWord("the", 2.55, 2.7),
        AsrWord("newspapers", 2.7, 3.2),
        AsrWord("Because", 4.0, 4.4),
    ]

    candidates = locator.locate(
        query_text="I don't read the newspapers",
        words=words,
        language="en",
        old_start=20.0,
        old_end=22.0,
        track_duration=60.0,
        threshold=0.5,
    )

    assert candidates
    assert candidates[0].matched_text == "I don't need the newspapers"
    assert candidates[0].start == 2.0
    assert candidates[0].end == 3.2
    assert candidates[0].phoneme_score > candidates[0].text_score


def test_locate_supports_russian_normalization_without_exact_old_position(monkeypatch) -> None:
    locator = _locator()

    def fake_phonemize(self, text: str, language: str) -> str:
        return self._normalize_text(text, language).replace("ё", "е")

    monkeypatch.setattr(PhraseLocator, "_phonemize_text", fake_phonemize)
    words = [
        AsrWord("это", 10.0, 10.3),
        AsrWord("будет", 10.3, 10.8),
        AsrWord("не", 10.8, 11.0),
        AsrWord("сложно", 11.0, 11.6),
    ]

    candidates = locator.locate(
        query_text="это будет не сложно",
        words=words,
        language="ru",
        old_start=0.0,
        old_end=1.0,
        track_duration=120.0,
        threshold=0.5,
    )

    assert candidates
    assert candidates[0].matched_text == "это будет не сложно"


def test_locate_can_return_multiple_repeated_occurrences(monkeypatch) -> None:
    locator = _locator()

    def fake_phonemize(self, text: str, language: str) -> str:
        return self._normalize_text(text, language).replace("take", "check")

    monkeypatch.setattr(PhraseLocator, "_phonemize_text", fake_phonemize)
    words = [
        AsrWord("Take", 18.6, 18.9),
        AsrWord("me", 18.9, 19.1),
        AsrWord("out", 19.1, 19.5),
        AsrWord("Take", 22.1, 22.4),
        AsrWord("me", 22.4, 22.6),
        AsrWord("out", 22.6, 23.0),
    ]

    candidates = locator.locate(
        query_text="Check me out",
        words=words,
        language="en",
        old_start=8.0,
        old_end=9.0,
        track_duration=60.0,
        threshold=0.5,
        limit=4,
        keep_overlapping=True,
    )

    assert len(candidates) >= 2
    starts = [candidate.start for candidate in candidates]
    assert 18.6 in starts
    assert 22.1 in starts
