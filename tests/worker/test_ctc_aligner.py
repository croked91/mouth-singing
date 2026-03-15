"""Unit tests for CTCAligner."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from karaoke_shared.models.track import SyllableTiming
from karaoke_shared.utils.syllabifier import Syllabifier


def _make_aligner():
    """Create a CTCAligner with mocked CTC model."""
    with patch("ctc_forced_aligner.AlignmentSingleton") as mock_cls:
        mock_singleton = MagicMock()
        mock_singleton.alignment_model = MagicMock()
        mock_singleton.alignment_tokenizer = MagicMock()
        mock_cls.return_value = mock_singleton

        from worker.common.ctc_aligner import CTCAligner

        return CTCAligner(
            syllabifier=Syllabifier(),
            min_frames_for_char=10,
        )


class TestCTCAlignerUnit:
    """Unit tests with mocked CTC model."""

    def test_init_loads_model(self):
        aligner = _make_aligner()
        assert aligner._model is not None
        assert aligner._tokenizer is not None

    def test_align_empty_lyrics_raises(self):
        aligner = _make_aligner()
        with pytest.raises(ValueError, match="empty"):
            aligner.align("/fake/vocals.wav", "", "ru")

    def test_align_whitespace_lyrics_raises(self):
        aligner = _make_aligner()
        with pytest.raises(ValueError, match="empty"):
            aligner.align("/fake/vocals.wav", "   \n  ", "ru")

    def test_lang_flags_ru(self):
        from worker.common.ctc_aligner import CTCAligner
        iso3, romanize = CTCAligner._lang_flags("ru")
        assert iso3 == "rus"
        assert romanize is True

    def test_lang_flags_en(self):
        from worker.common.ctc_aligner import CTCAligner
        iso3, romanize = CTCAligner._lang_flags("en")
        assert iso3 == "eng"
        assert romanize is False

    def test_lang_flags_unknown(self):
        from worker.common.ctc_aligner import CTCAligner
        iso3, romanize = CTCAligner._lang_flags("fr")
        assert iso3 == "eng"
        assert romanize is True

    def test_time_to_frame(self):
        from worker.common.ctc_aligner import CTCAligner
        assert CTCAligner._time_to_frame(1.0, 20) == 50
        assert CTCAligner._time_to_frame(0.0, 20) == 0
        assert CTCAligner._time_to_frame(0.5, 20) == 25

    def test_proportional_syllables(self):
        """Proportional fallback produces correct syllables."""
        aligner = _make_aligner()
        timings = aligner._proportional_syllables(
            "привет", 1.0, 2.0, "ru", " ",
        )
        assert len(timings) >= 1
        assert all(isinstance(t, SyllableTiming) for t in timings)
        assert timings[0].syllable.startswith(" ")
        assert timings[0].start >= 1.0
        assert timings[-1].end <= 2.001

    def test_proportional_syllables_single(self):
        """Single-syllable word returns one timing."""
        aligner = _make_aligner()
        timings = aligner._proportional_syllables(
            "да", 0.0, 0.5, "ru", "",
        )
        assert len(timings) == 1
        assert timings[0].syllable == "да"
        assert timings[0].start == 0.0
        assert timings[0].end == 0.5

    def test_syllables_from_char_timings_success(self):
        """Char timings correctly assemble into syllables."""
        aligner = _make_aligner()

        char_timings = [
            {"text": "п", "start": 0.0, "end": 0.05},
            {"text": "р", "start": 0.05, "end": 0.10},
            {"text": "и", "start": 0.10, "end": 0.15},
            {"text": "в", "start": 0.15, "end": 0.20},
            {"text": "е", "start": 0.20, "end": 0.25},
            {"text": "т", "start": 0.25, "end": 0.30},
        ]

        timings = aligner._syllables_from_char_timings(
            char_timings, "привет", 1.0, 1.5, "ru", "",
        )

        assert timings is not None
        assert len(timings) >= 1
        assert all(isinstance(t, SyllableTiming) for t in timings)

    def test_syllables_from_char_timings_insufficient_chars(self):
        """Not enough char timings returns None."""
        aligner = _make_aligner()

        char_timings = [
            {"text": "п", "start": 0.0, "end": 0.05},
        ]

        result = aligner._syllables_from_char_timings(
            char_timings, "привет", 1.0, 1.5, "ru", "",
        )
        assert result is None


class TestCTCAlignerIntegration:
    """Integration test with real CTC model — requires ctc-forced-aligner."""

    @pytest.fixture
    def aligner(self):
        """Real CTCAligner (loads MMS-300m, ~5s)."""
        try:
            from worker.common.ctc_aligner import CTCAligner
            return CTCAligner(syllabifier=Syllabifier())
        except Exception:
            pytest.skip("ctc-forced-aligner not available")

    def test_align_real_track(self, aligner, track1_vocals, track1_lyrics, track1_meta):
        """Full alignment on test track 1 produces valid syllable timings."""
        timings, stats = aligner.align(
            track1_vocals,
            track1_lyrics,
            track1_meta["language"],
        )

        assert len(timings) > 0
        assert stats.total_words > 0
        assert stats.char_level_used > 0
        assert stats.char_level_used / stats.total_words > 0.3

        for i in range(1, len(timings)):
            assert timings[i].start >= timings[i - 1].start

        for t in timings:
            assert t.end >= t.start
