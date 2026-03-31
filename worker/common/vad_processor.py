"""VAD processor — removes silence from vocal WAV files before ASR.

Uses librosa.effects.split to detect voiced segments and concatenates them
into a single cleaned WAV file at 16kHz mono (required by faster-whisper).
"""

from __future__ import annotations

import pathlib
import time

import structlog

logger = structlog.get_logger(__name__)


class VADProcessor:
    """Remove silence from a vocal WAV file.

    Args:
        top_db: Threshold in dB below peak for silence detection.
                35 dB works well for vocals; lower = stricter.
    """

    def __init__(self, top_db: int = 35) -> None:
        self._top_db = top_db

    def process(self, vocals_path: str) -> str:
        """Trim silence and save the result.

        Args:
            vocals_path: Absolute path to vocals.wav from UVR.

        Returns:
            Path to cleaned WAV (16kHz mono PCM_16), or the original
            vocals_path if loading fails or result is too short (< 1s).
        """
        import numpy as np

        try:
            import librosa
            import soundfile as sf
        except Exception as exc:
            logger.warning("vad_import_failed", error=str(exc))
            return vocals_path

        logger.info("vad_starting", vocals_path=vocals_path)
        t0 = time.monotonic()

        try:
            y, sr = librosa.load(vocals_path, sr=16000, mono=True)
        except Exception as exc:
            logger.warning("vad_load_failed", path=vocals_path, error=str(exc))
            return vocals_path

        intervals = librosa.effects.split(
            y, top_db=self._top_db, frame_length=2048, hop_length=512
        )

        if len(intervals) == 0:
            logger.warning("vad_no_voiced_segments", path=vocals_path)
            return vocals_path

        voiced = [y[start:end] for start, end in intervals]
        cleaned = np.concatenate(voiced)

        if len(cleaned) / 16000 < 1.0:
            logger.warning("vad_result_too_short", duration_sec=len(cleaned) / 16000)
            return vocals_path

        out_path = str(pathlib.Path(vocals_path).parent / "cleaned_vocals.wav")
        sf.write(out_path, cleaned, 16000, subtype="PCM_16")

        logger.info(
            "vad_completed",
            original_sec=len(y) / 16000,
            cleaned_sec=len(cleaned) / 16000,
            reduction_pct=round((1 - len(cleaned) / len(y)) * 100, 1),
            duration_sec=round(time.monotonic() - t0, 2),
        )
        return out_path
