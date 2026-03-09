"""Unit tests for WhisperTranscriber."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.pipeline.whisper_transcriber import WhisperResult


class TestWhisperResult:
    """Test WhisperResult dataclass."""

    def test_create(self):
        r = WhisperResult(text="hello world", language="en", confidence=0.8)
        assert r.text == "hello world"
        assert r.language == "en"
        assert r.confidence == 0.8


class TestWhisperTranscriber:
    """Tests for WhisperTranscriber with mocked model."""

    @patch("faster_whisper.WhisperModel")
    def test_transcribe_returns_result(self, mock_model_cls):
        """transcribe() joins segment texts and computes confidence."""
        from app.pipeline.whisper_transcriber import WhisperTranscriber

        mock_model = MagicMock()
        mock_model_cls.return_value = mock_model

        seg1 = MagicMock()
        seg1.text = " Hello world"
        seg1.avg_logprob = -0.3

        seg2 = MagicMock()
        seg2.text = " this is a test"
        seg2.avg_logprob = -0.5

        mock_info = MagicMock()
        mock_info.language = "en"

        mock_model.transcribe.return_value = (iter([seg1, seg2]), mock_info)

        transcriber = WhisperTranscriber(
            model_size="tiny", device="cpu", compute_type="int8"
        )
        result = transcriber.transcribe("/fake/audio.wav")

        assert isinstance(result, WhisperResult)
        assert "Hello world" in result.text
        assert "this is a test" in result.text
        assert result.language == "en"
        assert 0.0 <= result.confidence <= 1.0

    @patch("faster_whisper.WhisperModel")
    def test_transcribe_empty_segments(self, mock_model_cls):
        """Empty segments return empty text with confidence 0."""
        from app.pipeline.whisper_transcriber import WhisperTranscriber

        mock_model = MagicMock()
        mock_model_cls.return_value = mock_model

        mock_info = MagicMock()
        mock_info.language = "en"
        mock_model.transcribe.return_value = (iter([]), mock_info)

        transcriber = WhisperTranscriber(
            model_size="tiny", device="cpu", compute_type="int8"
        )
        result = transcriber.transcribe("/fake/audio.wav")

        assert result.text == ""
        assert result.confidence == 0.0

    @patch("faster_whisper.WhisperModel")
    def test_cleanup_releases_model(self, mock_model_cls):
        """cleanup() sets model to None."""
        from app.pipeline.whisper_transcriber import WhisperTranscriber

        mock_model = MagicMock()
        mock_model_cls.return_value = mock_model

        transcriber = WhisperTranscriber(
            model_size="tiny", device="cpu", compute_type="int8"
        )
        assert transcriber._model is not None

        transcriber.cleanup()
        assert transcriber._model is None
