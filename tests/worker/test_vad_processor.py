"""Unit tests for VADProcessor."""

from __future__ import annotations

import pathlib

import numpy as np
import pytest

from worker.common.vad_processor import VADProcessor


class TestVADProcessor:
    """Tests for VADProcessor.process()."""

    def test_process_returns_original_on_nonexistent_file(self):
        vad = VADProcessor()
        result = vad.process("/nonexistent/file.wav")
        assert result == "/nonexistent/file.wav"

    def test_process_normal_audio_creates_cleaned(self, tmp_path):
        """Normal audio with speech + silence should produce cleaned file."""
        import soundfile as sf

        sr = 16000
        # 2s of tone + 2s silence + 2s of tone
        t1 = np.sin(2 * np.pi * 440 * np.arange(sr * 2) / sr).astype(np.float32)
        silence = np.zeros(sr * 2, dtype=np.float32)
        t2 = np.sin(2 * np.pi * 440 * np.arange(sr * 2) / sr).astype(np.float32)
        audio = np.concatenate([t1, silence, t2])

        wav_path = str(tmp_path / "vocals.wav")
        sf.write(wav_path, audio, sr, subtype="PCM_16")

        vad = VADProcessor(top_db=20)
        result = vad.process(wav_path)

        assert result != wav_path
        assert pathlib.Path(result).name == "cleaned_vocals.wav"
        assert pathlib.Path(result).exists()

        # Cleaned should be shorter than original
        import librosa
        y_orig, _ = librosa.load(wav_path, sr=sr)
        y_clean, _ = librosa.load(result, sr=sr)
        assert len(y_clean) < len(y_orig)

    def test_process_returns_valid_path(self, tmp_path):
        """Even with very short audio, result path exists."""
        import soundfile as sf

        sr = 16000
        # 0.5s burst
        burst = np.sin(2 * np.pi * 440 * np.arange(int(sr * 0.5)) / sr).astype(np.float32)

        wav_path = str(tmp_path / "vocals.wav")
        sf.write(wav_path, burst, sr, subtype="PCM_16")

        vad = VADProcessor(top_db=20)
        result = vad.process(wav_path)
        assert pathlib.Path(result).exists()

    def test_custom_top_db(self):
        """VADProcessor accepts custom top_db."""
        vad = VADProcessor(top_db=25)
        assert vad._top_db == 25
