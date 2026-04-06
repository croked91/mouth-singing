"""VAD processor — removes silence from vocal WAV files before ASR.

Uses RMS energy detection via PyTorch to find voiced segments and
concatenates them into a single cleaned WAV file at 16kHz mono.
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
        import soundfile as sf
        import torch
        import torchaudio.functional as F

        logger.info("vad_starting", vocals_path=vocals_path)
        t0 = time.monotonic()

        try:
            data, sr = sf.read(vocals_path, dtype="float32")
            if data.ndim > 1:
                data = data.mean(axis=1)
            if sr != 16000:
                y_tensor = F.resample(torch.from_numpy(data), sr, 16000)
                y = y_tensor.numpy()
            else:
                y = data
        except Exception as exc:
            logger.warning("vad_load_failed", path=vocals_path, error=str(exc))
            return vocals_path

        # RMS energy VAD via PyTorch (CPU).
        frame_length = 2048
        hop_length = 512
        yt = torch.from_numpy(y)
        frames = yt.unfold(0, frame_length, hop_length)
        rms = frames.pow(2).mean(dim=1).sqrt()
        threshold = rms.max() * 10 ** (-self._top_db / 20)
        is_voiced = rms > threshold

        # Convert frame mask to sample intervals.
        voiced_np = is_voiced.numpy()
        diff = np.diff(voiced_np.astype(np.int8), prepend=0, append=0)
        starts = np.where(diff == 1)[0]
        ends = np.where(diff == -1)[0]

        if len(starts) == 0:
            logger.warning("vad_no_voiced_segments", path=vocals_path)
            return vocals_path

        # Convert frame indices to sample indices.
        intervals = [
            (s * hop_length, min(e * hop_length + frame_length, len(y)))
            for s, e in zip(starts, ends)
        ]

        voiced = [y[s:e] for s, e in intervals]
        cleaned = np.concatenate(voiced)

        if len(cleaned) / 16000 < 1.0:
            logger.warning("vad_result_too_short", duration_sec=len(cleaned) / 16000)
            return vocals_path

        track_id = pathlib.Path(vocals_path).stem.split("_")[0]
        out_path = str(pathlib.Path(vocals_path).parent / f"cleaned_vocals_{track_id}.wav")
        sf.write(out_path, cleaned, 16000, subtype="PCM_16")

        logger.info(
            "vad_completed",
            original_sec=len(y) / 16000,
            cleaned_sec=len(cleaned) / 16000,
            reduction_pct=round((1 - len(cleaned) / len(y)) * 100, 1),
            duration_sec=round(time.monotonic() - t0, 2),
        )
        return out_path
