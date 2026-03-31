"""Audio feature extractor for track recommendation.

Produces a 45-dimensional L2-normalised vector from an audio file:
  - MFCC (13)
  - Chroma (12)
  - Spectral Contrast (7)
  - Tonnetz (6)
  - Tempo, spectral centroid, spectral bandwidth, spectral rolloff,
    zero-crossing rate, RMS energy, spectral flatness (7)

After raw feature extraction the vector undergoes:
1. L2-normalisation (standard for cosine similarity).
2. Z-score normalisation using pre-computed catalog statistics so that
   every feature dimension contributes equally to cosine distance.
3. Final L2-renormalisation.

This module is synchronous. Call from async code via ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import structlog

logger = structlog.get_logger(__name__)

_EXPECTED_DIM = 45


class FeatureExtractor:
    """Extracts a 45-dimensional audio feature vector from an audio file.

    Relies on ``librosa`` for all DSP operations. The resulting vector is
    L2-normalised so that cosine similarity equals dot-product similarity in
    QDrant.

    Args:
        normalization_stats_path: Optional path to a JSON file containing
            ``{"mean": [...], "std": [...]}`` computed over the catalog.
            When provided, the vector is z-score normalised before the
            final L2-normalisation step.
    """

    def __init__(self, normalization_stats_path: str | None = None) -> None:
        self._norm_mean: np.ndarray | None = None
        self._norm_std: np.ndarray | None = None

        if normalization_stats_path is not None:
            p = Path(normalization_stats_path)
            if p.exists():
                data = json.loads(p.read_text())
                self._norm_mean = np.array(data["mean"], dtype=np.float64)
                self._norm_std = np.array(data["std"], dtype=np.float64)
                # Guard against zero-variance dimensions.
                self._norm_std = np.where(self._norm_std < 1e-8, 1.0, self._norm_std)
                logger.info(
                    "feature_extractor.normalization_stats_loaded",
                    path=normalization_stats_path,
                )
            else:
                logger.warning(
                    "feature_extractor.normalization_stats_not_found",
                    path=normalization_stats_path,
                )

    def extract(self, audio_path: str) -> list[float]:
        """Extract features from an audio file and return a 45-d vector.

        Args:
            audio_path: Absolute path to a WAV (or any librosa-compatible)
                        audio file.

        Returns:
            List of exactly 45 floats, L2-normalised. Returns a zero vector
            if the audio is silent or too short to analyse.
        """
        import librosa  # lazy import — not required at module-import time

        logger.info("feature_extraction_starting", path=audio_path)
        t0 = time.monotonic()

        try:
            y, sr = librosa.load(audio_path, sr=None, mono=True)
        except Exception:
            logger.exception("feature_extractor.load_failed", path=audio_path)
            return [0.0] * _EXPECTED_DIM

        if len(y) < 512:
            logger.warning("feature_extractor.audio_too_short", path=audio_path, samples=len(y))
            return [0.0] * _EXPECTED_DIM

        try:
            vector = self._compute(y, sr)
        except Exception:
            logger.exception("feature_extractor.compute_failed", path=audio_path)
            return [0.0] * _EXPECTED_DIM

        logger.info(
            "feature_extraction_completed",
            path=audio_path,
            duration_sec=round(time.monotonic() - t0, 2),
        )

        # Post-hoc z-score normalisation using catalog statistics.
        if self._norm_mean is not None and self._norm_std is not None:
            arr = np.array(vector, dtype=np.float64)
            arr = (arr - self._norm_mean) / self._norm_std
            norm = np.linalg.norm(arr)
            if norm > 1e-8:
                arr = arr / norm
            vector = arr.tolist()

        return vector

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute(y: np.ndarray, sr: int) -> list[float]:
        import librosa

        hop_length = 512

        # MFCC — 13 coefficients → mean across time → 13 dims
        mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13, hop_length=hop_length)
        mfcc_mean = mfcc.mean(axis=1)  # (13,)

        # Chroma — 12 semitones → mean → 12 dims
        chroma = librosa.feature.chroma_stft(y=y, sr=sr, hop_length=hop_length)
        chroma_mean = chroma.mean(axis=1)  # (12,)

        # Spectral Contrast — 7 bands → mean → 7 dims
        contrast = librosa.feature.spectral_contrast(y=y, sr=sr, hop_length=hop_length)
        contrast_mean = contrast.mean(axis=1)  # (7,)

        # Tonnetz — 6 dims → mean → 6 dims
        y_harmonic = librosa.effects.harmonic(y)
        tonnetz = librosa.feature.tonnetz(y=y_harmonic, sr=sr)
        tonnetz_mean = tonnetz.mean(axis=1)  # (6,)

        # Tempo — 1 dim
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr, hop_length=hop_length)
        tempo_scalar = np.array([float(np.atleast_1d(tempo)[0])])  # (1,)

        # Spectral centroid — 1 dim
        centroid = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=hop_length)
        centroid_mean = np.array([centroid.mean()])  # (1,)

        # Spectral bandwidth — 1 dim
        bandwidth = librosa.feature.spectral_bandwidth(y=y, sr=sr, hop_length=hop_length)
        bandwidth_mean = np.array([bandwidth.mean()])  # (1,)

        # Spectral rolloff — 1 dim
        rolloff = librosa.feature.spectral_rolloff(y=y, sr=sr, hop_length=hop_length)
        rolloff_mean = np.array([rolloff.mean()])  # (1,)

        # Zero-crossing rate — 1 dim
        zcr = librosa.feature.zero_crossing_rate(y, hop_length=hop_length)
        zcr_mean = np.array([zcr.mean()])  # (1,)

        # RMS energy — 1 dim
        rms = librosa.feature.rms(y=y, hop_length=hop_length)
        rms_mean = np.array([rms.mean()])  # (1,)

        # Spectral flatness — 1 dim
        flatness = librosa.feature.spectral_flatness(y=y, hop_length=hop_length)
        flatness_mean = np.array([flatness.mean()])  # (1,)

        raw = np.concatenate([
            mfcc_mean,       # 13
            chroma_mean,     # 12
            contrast_mean,   # 7
            tonnetz_mean,    # 6
            tempo_scalar,    # 1
            centroid_mean,   # 1
            bandwidth_mean,  # 1
            rolloff_mean,    # 1
            zcr_mean,        # 1
            rms_mean,        # 1
            flatness_mean,   # 1
        ])  # total: 45

        # Replace any NaN / Inf that can arise from silent segments.
        raw = np.nan_to_num(raw, nan=0.0, posinf=0.0, neginf=0.0)

        norm = np.linalg.norm(raw)
        if norm > 0.0:
            raw = raw / norm

        return raw.tolist()
