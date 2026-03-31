"""Unit tests for TorchCTCAligner.

Tests cover tokenization, span grouping, syllable timing generation,
and cleanup — all without loading the real ML model or requiring
torch/torchaudio in the test environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch
import sys

import pytest

from karaoke_shared.models.track import SyllableTiming

# Provide lightweight stubs for torch/torchaudio so the module can be
# imported in a test venv that does not have them installed.
_torch_stub = MagicMock()
_torchaudio_stub = MagicMock()
_need_stubs = "torch" not in sys.modules
if _need_stubs:
    sys.modules["torch"] = _torch_stub
    sys.modules["torchaudio"] = _torchaudio_stub
    sys.modules["torchaudio.functional"] = _torchaudio_stub.functional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class _FakeTokenSpan:
    """Mimics torchaudio TokenSpan returned by merge_tokens."""
    token: int
    start: int
    end: int
    score: float = 0.0


def _make_aligner(device: str = "cpu"):
    """Create TorchCTCAligner without loading the real model."""
    from worker.gpu.torch_ctc_aligner import TorchCTCAligner

    aligner = TorchCTCAligner(device=device, model_cache_dir=None)
    # Inject a fake dictionary (matches MMS-300M vocab layout).
    aligner._dictionary = {
        "a": 4, "b": 20, "c": 23, "d": 16, "e": 6, "f": 27,
        "g": 17, "h": 18, "i": 5, "j": 25, "k": 14, "l": 15,
        "m": 13, "n": 7, "o": 8, "p": 21, "q": 29, "r": 12,
        "s": 11, "t": 10, "u": 9, "v": 24, "w": 22, "x": 30,
        "y": 19, "z": 26, "'": 28,
    }
    aligner._blank_idx = 0
    return aligner


# ---------------------------------------------------------------------------
# Tests: input validation
# ---------------------------------------------------------------------------

class TestAlignValidation:
    def test_empty_lyrics_raises(self):
        aligner = _make_aligner()
        with pytest.raises(ValueError, match="empty"):
            aligner.align("/fake/vocals.mp3", "", "ru")

    def test_whitespace_lyrics_raises(self):
        aligner = _make_aligner()
        with pytest.raises(ValueError, match="empty"):
            aligner.align("/fake/vocals.mp3", "   \n  ", "ru")


# ---------------------------------------------------------------------------
# Tests: tokenization
# ---------------------------------------------------------------------------

class TestTokenizeLyrics:
    def test_english_text(self):
        aligner = _make_aligner()
        words, transcript, flags = aligner._tokenize_lyrics("hello world", "en")
        assert words == ["hello", "world"]
        assert transcript == [["h", "e", "l", "l", "o"], ["w", "o", "r", "l", "d"]]
        assert flags == [True, False]

    def test_russian_romanized(self):
        aligner = _make_aligner()
        words, transcript, flags = aligner._tokenize_lyrics("привет", "ru")
        assert words == ["привет"]
        assert len(transcript) == 1
        # unidecode("привет") = "privet"
        assert transcript[0] == ["p", "r", "i", "v", "e", "t"]
        assert flags == [True]

    def test_multiline_first_flags(self):
        aligner = _make_aligner()
        lyrics = "hello world\ngoodbye moon"
        words, transcript, flags = aligner._tokenize_lyrics(lyrics, "en")
        assert words == ["hello", "world", "goodbye", "moon"]
        assert flags == [True, False, True, False]

    def test_empty_lines_skipped(self):
        aligner = _make_aligner()
        lyrics = "hello\n\n\nworld"
        words, transcript, flags = aligner._tokenize_lyrics(lyrics, "en")
        assert words == ["hello", "world"]
        assert flags == [True, True]

    def test_non_dict_chars_filtered(self):
        aligner = _make_aligner()
        # Numbers and punctuation should be filtered out.
        words, transcript, flags = aligner._tokenize_lyrics("hello123!", "en")
        assert words == ["hello123!"]
        assert transcript == [["h", "e", "l", "l", "o"]]

    def test_word_with_no_valid_chars_skipped(self):
        aligner = _make_aligner()
        words, transcript, flags = aligner._tokenize_lyrics("123 hello", "en")
        assert words == ["hello"]
        assert transcript == [["h", "e", "l", "l", "o"]]
        assert flags == [True]


# ---------------------------------------------------------------------------
# Tests: unflatten
# ---------------------------------------------------------------------------

class TestUnflatten:
    def test_basic(self):
        from worker.gpu.torch_ctc_aligner import TorchCTCAligner

        spans = [_FakeTokenSpan(i, i * 10, i * 10 + 5) for i in range(7)]
        result = TorchCTCAligner._unflatten(spans, [3, 2, 2])
        assert len(result) == 3
        assert len(result[0]) == 3
        assert len(result[1]) == 2
        assert len(result[2]) == 2

    def test_truncated_spans(self):
        from worker.gpu.torch_ctc_aligner import TorchCTCAligner

        spans = [_FakeTokenSpan(i, i * 10, i * 10 + 5) for i in range(3)]
        # Request more chars than available.
        result = TorchCTCAligner._unflatten(spans, [2, 5])
        assert len(result) == 1  # Only first word fits

    def test_empty(self):
        from worker.gpu.torch_ctc_aligner import TorchCTCAligner

        result = TorchCTCAligner._unflatten([], [3, 2])
        assert result == []


# ---------------------------------------------------------------------------
# Tests: syllable timing generation
# ---------------------------------------------------------------------------

class TestToSyllableTimings:
    def _make_word_spans(self, words_data):
        """Create word spans from [(start, end), ...] pairs."""
        result = []
        for start, end in words_data:
            result.append([_FakeTokenSpan(0, start, end)])
        return result

    def test_basic_prefix_spacing(self):
        aligner = _make_aligner()
        word_spans = self._make_word_spans([(0, 100), (100, 200), (200, 300)])
        timings, stats = aligner._to_syllable_timings(
            ["Hi", "my", "friend"],
            word_spans,
            ratio=0.01,
            language="en",
            first_flags=[True, False, False],
        )
        assert timings[0].syllable == "Hi"  # First word, no prefix
        assert timings[1].syllable.startswith(" ")  # Space prefix
        assert stats.total_words == 3

    def test_newline_prefix_for_first_in_line(self):
        aligner = _make_aligner()
        word_spans = self._make_word_spans([(0, 100), (100, 200), (200, 300)])
        timings, stats = aligner._to_syllable_timings(
            ["Hello", "world", "Goodbye"],
            word_spans,
            ratio=0.01,
            language="en",
            first_flags=[True, False, True],
        )
        assert timings[0].syllable == "Hel"  # No prefix (first overall)
        # "world" has space prefix
        world_timing = [t for t in timings if "world" in t.syllable][0]
        assert world_timing.syllable.startswith(" ")
        # "Goodbye" is first_in_line → \n prefix
        goodbye_timing = [t for t in timings if "Good" in t.syllable][0]
        assert goodbye_timing.syllable.startswith("\n")

    def test_syllable_splitting(self):
        aligner = _make_aligner()
        word_spans = self._make_word_spans([(0, 100)])
        timings, stats = aligner._to_syllable_timings(
            ["привет"],
            word_spans,
            ratio=0.01,
            language="ru",
        )
        # "привет" → ["при", "вет"] by pyphen
        assert len(timings) >= 2
        assert timings[0].start == 0.0
        assert timings[-1].end > 0.0

    def test_single_syllable_word(self):
        aligner = _make_aligner()
        word_spans = self._make_word_spans([(0, 100)])
        timings, stats = aligner._to_syllable_timings(
            ["cat"],
            word_spans,
            ratio=0.01,
            language="en",
        )
        assert len(timings) == 1
        assert timings[0].syllable == "cat"

    def test_empty_spans_skipped(self):
        aligner = _make_aligner()
        word_spans = [[], self._make_word_spans([(100, 200)])[0]]
        timings, stats = aligner._to_syllable_timings(
            ["bad", "good"],
            word_spans,
            ratio=0.01,
            language="en",
            first_flags=[True, False],
        )
        # First word skipped (empty spans), second word is first displayed.
        assert len(timings) >= 1

    def test_no_first_flags(self):
        """When first_flags is None, all non-first words get space prefix."""
        aligner = _make_aligner()
        word_spans = self._make_word_spans([(0, 100), (100, 200)])
        timings, stats = aligner._to_syllable_timings(
            ["hello", "world"],
            word_spans,
            ratio=0.01,
            language="en",
            first_flags=None,
        )
        assert timings[0].syllable == "hel"  # No prefix
        world_timing = [t for t in timings if "world" in t.syllable][0]
        assert world_timing.syllable.startswith(" ")


# ---------------------------------------------------------------------------
# Tests: cleanup
# ---------------------------------------------------------------------------

class TestCleanup:
    def test_cleanup_releases_model(self):
        aligner = _make_aligner()
        aligner._model = MagicMock()
        aligner.cleanup()
        assert aligner._model is None

    def test_cleanup_noop_when_no_model(self):
        aligner = _make_aligner()
        assert aligner._model is None
        aligner.cleanup()  # Should not raise
        assert aligner._model is None
