"""Unit tests for WhisperTranscriber."""

from __future__ import annotations

from worker.gpu.whisper_transcriber import WhisperResult


class TestWhisperResult:
    """Test WhisperResult dataclass."""

    def test_create(self):
        r = WhisperResult(text="hello world", language="en", confidence=0.8)
        assert r.text == "hello world"
        assert r.language == "en"
        assert r.confidence == 0.8
