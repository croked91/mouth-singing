"""Unit tests for FeatureExtractor.

Strategy
--------
- ``librosa`` is NOT installed in the test environment. The module uses a
  lazy ``import librosa`` inside each method, so we stub out the entire
  ``librosa`` namespace in ``sys.modules`` before importing the module under
  test.  Every librosa call is thus intercepted by MagicMock objects.
- All tests are synchronous (FeatureExtractor.extract is synchronous by
  design; the docstring says "Call from async code via asyncio.to_thread").
- Table-driven tests are used wherever multiple input variants share the same
  assertion logic.
"""

from __future__ import annotations

import importlib
import sys
import types
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Stub out librosa BEFORE any import of the module under test so the lazy
# ``import librosa`` inside FeatureExtractor methods hits the stub.
# ---------------------------------------------------------------------------

_LIBROSA_STUB = types.ModuleType("librosa")
_LIBROSA_FEATURE_STUB = types.ModuleType("librosa.feature")
_LIBROSA_EFFECTS_STUB = types.ModuleType("librosa.effects")
_LIBROSA_BEAT_STUB = types.ModuleType("librosa.beat")

# Top-level librosa namespace
_LIBROSA_STUB.load = MagicMock()  # type: ignore[attr-defined]
_LIBROSA_STUB.feature = _LIBROSA_FEATURE_STUB  # type: ignore[attr-defined]
_LIBROSA_STUB.effects = _LIBROSA_EFFECTS_STUB  # type: ignore[attr-defined]
_LIBROSA_STUB.beat = _LIBROSA_BEAT_STUB  # type: ignore[attr-defined]

# librosa.feature sub-functions (each returns a 2-D array before mean())
_LIBROSA_FEATURE_STUB.mfcc = MagicMock()  # type: ignore[attr-defined]
_LIBROSA_FEATURE_STUB.chroma_stft = MagicMock()  # type: ignore[attr-defined]
_LIBROSA_FEATURE_STUB.spectral_contrast = MagicMock()  # type: ignore[attr-defined]
_LIBROSA_FEATURE_STUB.tonnetz = MagicMock()  # type: ignore[attr-defined]
_LIBROSA_FEATURE_STUB.spectral_centroid = MagicMock()  # type: ignore[attr-defined]
_LIBROSA_FEATURE_STUB.spectral_bandwidth = MagicMock()  # type: ignore[attr-defined]
_LIBROSA_FEATURE_STUB.spectral_rolloff = MagicMock()  # type: ignore[attr-defined]
_LIBROSA_FEATURE_STUB.zero_crossing_rate = MagicMock()  # type: ignore[attr-defined]
_LIBROSA_FEATURE_STUB.rms = MagicMock()  # type: ignore[attr-defined]
_LIBROSA_FEATURE_STUB.spectral_flatness = MagicMock()  # type: ignore[attr-defined]

# librosa.effects
_LIBROSA_EFFECTS_STUB.harmonic = MagicMock()  # type: ignore[attr-defined]

# librosa.beat
_LIBROSA_BEAT_STUB.beat_track = MagicMock()  # type: ignore[attr-defined]

sys.modules.setdefault("librosa", _LIBROSA_STUB)
sys.modules.setdefault("librosa.feature", _LIBROSA_FEATURE_STUB)
sys.modules.setdefault("librosa.effects", _LIBROSA_EFFECTS_STUB)
sys.modules.setdefault("librosa.beat", _LIBROSA_BEAT_STUB)

# ---------------------------------------------------------------------------
# Now import the module under test.  structlog is already available in the
# venv; if it weren't we would stub it out too.
# ---------------------------------------------------------------------------

import importlib.util as _ilu
import pathlib as _pathlib

_SHARED_ROOT = _pathlib.Path(__file__).parent.parent / "shared"
_FE_SPEC = _ilu.spec_from_file_location(
    "_feature_extractor_mod",
    str(_SHARED_ROOT / "karaoke_shared" / "ml" / "feature_extractor.py"),
    submodule_search_locations=[],
)
assert _FE_SPEC is not None and _FE_SPEC.loader is not None
_fe_mod = _ilu.module_from_spec(_FE_SPEC)
sys.modules["_feature_extractor_mod"] = _fe_mod
_FE_SPEC.loader.exec_module(_fe_mod)

FeatureExtractor = _fe_mod.FeatureExtractor
_EXPECTED_DIM = 45


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _configure_librosa_feature_mocks(n_frames: int = 10) -> None:
    """Reset all librosa feature mocks to return sensible arrays.

    Each feature function returns an ndarray of the documented shape so that
    ``.mean(axis=1)`` produces an array of the correct length.

    Args:
        n_frames: Number of time frames in the mock feature matrices.
    """
    import librosa  # resolves to stub

    rng = np.random.default_rng(42)

    # 2-D arrays: (n_features, n_frames) — axis=1 mean gives (n_features,)
    librosa.feature.mfcc.return_value = rng.random((13, n_frames)).astype(np.float32)
    librosa.feature.chroma_stft.return_value = rng.random((12, n_frames)).astype(np.float32)
    librosa.feature.spectral_contrast.return_value = rng.random((7, n_frames)).astype(np.float32)
    librosa.feature.tonnetz.return_value = rng.random((6, n_frames)).astype(np.float32)
    librosa.feature.spectral_centroid.return_value = rng.random((1, n_frames)).astype(np.float32)
    librosa.feature.spectral_bandwidth.return_value = rng.random((1, n_frames)).astype(np.float32)
    librosa.feature.spectral_rolloff.return_value = rng.random((1, n_frames)).astype(np.float32)
    librosa.feature.zero_crossing_rate.return_value = rng.random((1, n_frames)).astype(np.float32)
    librosa.feature.rms.return_value = rng.random((1, n_frames)).astype(np.float32)
    librosa.feature.spectral_flatness.return_value = rng.random((1, n_frames)).astype(np.float32)

    # beat_track returns (tempo_scalar_or_array, beat_frames_array)
    librosa.beat.beat_track.return_value = (np.float32(120.0), np.array([10, 20, 30]))

    # effects.harmonic just returns the input signal unchanged
    librosa.effects.harmonic.side_effect = lambda y: y


def _make_audio(n_samples: int = 22050) -> tuple[np.ndarray, int]:
    """Return a synthetic audio signal of the requested length."""
    rng = np.random.default_rng(0)
    return rng.random(n_samples).astype(np.float32), 22050


# ---------------------------------------------------------------------------
# Tests: vector dimension
# ---------------------------------------------------------------------------


class TestFeatureExtractorDimension:
    """The extract() method must always return exactly 45 floats."""

    def test_extract_returns_list_of_45_floats(self) -> None:
        """Happy-path: extract() returns a list with 45 elements."""
        import librosa  # stub

        y, sr = _make_audio()
        librosa.load.return_value = (y, sr)
        _configure_librosa_feature_mocks()

        result = FeatureExtractor().extract("/fake/audio.wav")

        assert isinstance(result, list)
        assert len(result) == _EXPECTED_DIM

    def test_extract_elements_are_floats(self) -> None:
        """Every element in the returned vector is a Python float."""
        import librosa

        y, sr = _make_audio()
        librosa.load.return_value = (y, sr)
        _configure_librosa_feature_mocks()

        result = FeatureExtractor().extract("/fake/audio.wav")

        assert all(isinstance(v, float) for v in result)

    @pytest.mark.parametrize("n_frames", [5, 10, 100])
    def test_extract_dimension_independent_of_audio_length(self, n_frames: int) -> None:
        """Dimension is always 45 regardless of the number of time frames."""
        import librosa

        # Adjust audio length proportionally (n_frames * hop_length)
        n_samples = n_frames * 512 + 512
        y, sr = _make_audio(n_samples)
        librosa.load.return_value = (y, sr)
        _configure_librosa_feature_mocks(n_frames=n_frames)

        result = FeatureExtractor().extract("/fake/audio.wav")

        assert len(result) == _EXPECTED_DIM


# ---------------------------------------------------------------------------
# Tests: L2 normalisation
# ---------------------------------------------------------------------------


class TestFeatureExtractorL2Normalisation:
    """The returned vector must have L2 norm ≈ 1.0 for non-silent audio."""

    def test_extract_vector_is_unit_norm(self) -> None:
        """The L2 norm of the returned vector is approximately 1.0."""
        import librosa

        y, sr = _make_audio()
        librosa.load.return_value = (y, sr)
        _configure_librosa_feature_mocks()

        result = FeatureExtractor().extract("/fake/audio.wav")

        norm = np.linalg.norm(result)
        assert pytest.approx(norm, abs=1e-5) == 1.0

    def test_extract_norm_close_to_one_with_varied_magnitudes(self) -> None:
        """L2 norm is 1.0 even when feature values span large magnitude ranges."""
        import librosa

        y, sr = _make_audio()
        librosa.load.return_value = (y, sr)
        _configure_librosa_feature_mocks()

        # Scale some feature arrays to large values to verify normalisation
        librosa.feature.mfcc.return_value *= 1000.0

        result = FeatureExtractor().extract("/fake/audio.wav")

        norm = np.linalg.norm(result)
        assert pytest.approx(norm, abs=1e-5) == 1.0


# ---------------------------------------------------------------------------
# Tests: edge cases that return a zero vector
# ---------------------------------------------------------------------------


class TestFeatureExtractorZeroVectorCases:
    """Cases where extract() must return a zero vector of length 45."""

    def _assert_zero_vector(self, result: list[float]) -> None:
        assert len(result) == _EXPECTED_DIM
        assert all(v == 0.0 for v in result), f"Expected zero vector, got {result[:5]}..."

    def test_audio_too_short_returns_zero_vector(self) -> None:
        """Audio shorter than 512 samples returns a zero vector."""
        import librosa

        short_audio = np.zeros(100, dtype=np.float32)  # < 512 samples
        librosa.load.return_value = (short_audio, 22050)

        result = FeatureExtractor().extract("/fake/short.wav")

        self._assert_zero_vector(result)

    @pytest.mark.parametrize("n_samples", [0, 1, 256, 511])
    def test_audio_shorter_than_512_returns_zero_vector(self, n_samples: int) -> None:
        """Any audio shorter than 512 samples returns a zero vector."""
        import librosa

        librosa.load.return_value = (np.zeros(n_samples, dtype=np.float32), 22050)

        result = FeatureExtractor().extract("/fake/short.wav")

        self._assert_zero_vector(result)

    def test_librosa_load_failure_returns_zero_vector(self) -> None:
        """If librosa.load raises any exception, extract() returns a zero vector."""
        import librosa

        librosa.load.side_effect = OSError("File not found")

        try:
            result = FeatureExtractor().extract("/fake/nonexistent.wav")
            self._assert_zero_vector(result)
        finally:
            librosa.load.side_effect = None

    @pytest.mark.parametrize(
        "exc_type, exc_msg",
        [
            (OSError, "No such file or directory"),
            (RuntimeError, "Unsupported format"),
            (ValueError, "Invalid sample rate"),
            (Exception, "Unknown error"),
        ],
    )
    def test_various_load_exceptions_return_zero_vector(
        self, exc_type: type, exc_msg: str
    ) -> None:
        """Any exception from librosa.load results in a zero vector."""
        import librosa

        librosa.load.side_effect = exc_type(exc_msg)

        try:
            result = FeatureExtractor().extract("/fake/audio.wav")
            self._assert_zero_vector(result)
        finally:
            librosa.load.side_effect = None

    def test_silent_audio_returns_zero_vector(self) -> None:
        """Completely silent audio (all-zero samples) returns a zero vector.

        After feature extraction, all-zero features produce a raw vector of
        zeros; the normalisation step preserves the zero vector (norm == 0
        branch in _compute).
        """
        import librosa

        # Sufficient length to pass the < 512 guard
        silent_audio = np.zeros(22050, dtype=np.float32)
        librosa.load.return_value = (silent_audio, 22050)

        # Configure feature mocks to return all-zero arrays (simulating silence)
        n_frames = 10
        librosa.feature.mfcc.return_value = np.zeros((13, n_frames), dtype=np.float32)
        librosa.feature.chroma_stft.return_value = np.zeros((12, n_frames), dtype=np.float32)
        librosa.feature.spectral_contrast.return_value = np.zeros((7, n_frames), dtype=np.float32)
        librosa.feature.tonnetz.return_value = np.zeros((6, n_frames), dtype=np.float32)
        librosa.feature.spectral_centroid.return_value = np.zeros((1, n_frames), dtype=np.float32)
        librosa.feature.spectral_bandwidth.return_value = np.zeros((1, n_frames), dtype=np.float32)
        librosa.feature.spectral_rolloff.return_value = np.zeros((1, n_frames), dtype=np.float32)
        librosa.feature.zero_crossing_rate.return_value = np.zeros((1, n_frames), dtype=np.float32)
        librosa.feature.rms.return_value = np.zeros((1, n_frames), dtype=np.float32)
        librosa.feature.spectral_flatness.return_value = np.zeros((1, n_frames), dtype=np.float32)
        librosa.beat.beat_track.return_value = (np.float32(0.0), np.array([]))
        librosa.effects.harmonic.side_effect = lambda y: y

        result = FeatureExtractor().extract("/fake/silent.wav")

        self._assert_zero_vector(result)


# ---------------------------------------------------------------------------
# Tests: synchronous API
# ---------------------------------------------------------------------------


class TestFeatureExtractorSync:
    """extract() must be synchronous — not a coroutine."""

    def test_extract_is_not_a_coroutine(self) -> None:
        """Calling extract() returns a list, not a coroutine object."""
        import asyncio
        import librosa

        y, sr = _make_audio()
        librosa.load.return_value = (y, sr)
        _configure_librosa_feature_mocks()

        result = FeatureExtractor().extract("/fake/audio.wav")

        assert not asyncio.iscoroutine(result), (
            "extract() returned a coroutine — it must be synchronous"
        )

    def test_extract_does_not_require_event_loop(self) -> None:
        """extract() can be called outside of an async context."""
        import librosa

        y, sr = _make_audio()
        librosa.load.return_value = (y, sr)
        _configure_librosa_feature_mocks()

        # If this raises RuntimeError("no running event loop"), the test fails.
        result = FeatureExtractor().extract("/fake/audio.wav")

        assert isinstance(result, list)
