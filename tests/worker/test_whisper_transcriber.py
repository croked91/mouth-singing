"""Unit tests for WhisperTranscriber."""

from __future__ import annotations

from worker.gpu.whisper_transcriber import WhisperResult


class TestWhisperResult:
    """Test WhisperResult dataclass."""

    def test_create(self):
        r = WhisperResult(text="hello world", language="en")
        assert r.text == "hello world"
        assert r.language == "en"
