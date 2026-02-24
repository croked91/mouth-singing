"""Unit tests for LyricEmbedder.

Strategy
--------
- ``sentence_transformers`` is NOT installed in the test environment.  The
  constructor does ``from sentence_transformers import SentenceTransformer``
  lazily, so we insert a stub module into ``sys.modules`` before importing the
  module under test.
- The SentenceTransformer constructor is replaced with a MagicMock that
  returns a controllable mock model instance.  The mock model's ``encode``
  method returns numpy arrays of shape ``(n_chunks, 384)`` to match the real
  API.
- The model's ``tokenizer`` attribute is also mocked so that ``_chunk_text``
  can be exercised through the public ``embed`` interface.
- All tests are synchronous (LyricEmbedder.embed is synchronous by design).
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Stub out sentence_transformers BEFORE importing the module under test.
# ---------------------------------------------------------------------------

_ST_STUB = types.ModuleType("sentence_transformers")
_ST_STUB.SentenceTransformer = MagicMock()  # type: ignore[attr-defined]
sys.modules.setdefault("sentence_transformers", _ST_STUB)

# ---------------------------------------------------------------------------
# Load the module under test via importlib so we don't pollute the shared
# ``karaoke_shared`` namespace on re-import.
# ---------------------------------------------------------------------------

import importlib.util as _ilu
import pathlib as _pathlib

_SHARED_ROOT = _pathlib.Path(__file__).parent.parent / "shared"
_LE_SPEC = _ilu.spec_from_file_location(
    "_lyric_embedder_mod",
    str(_SHARED_ROOT / "karaoke_shared" / "ml" / "lyric_embedder.py"),
    submodule_search_locations=[],
)
assert _LE_SPEC is not None and _LE_SPEC.loader is not None
_le_mod = _ilu.module_from_spec(_LE_SPEC)
sys.modules["_lyric_embedder_mod"] = _le_mod
_LE_SPEC.loader.exec_module(_le_mod)

LyricEmbedder = _le_mod.LyricEmbedder
_EMBEDDING_DIM = 384
_CHUNK_TOKENS = 256  # matches _CHUNK_TOKENS inside the module


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def _make_model_mock(embedding_dim: int = _EMBEDDING_DIM) -> MagicMock:
    """Build a SentenceTransformer mock that returns plausible embeddings.

    ``encode(sentences, ...)`` returns an ndarray of shape
    ``(len(sentences), embedding_dim)`` with random values so that the
    mean-pooling in ``embed()`` can be exercised.

    The ``tokenizer`` attribute is wired up so that ``_chunk_text`` can
    produce realistic chunking behaviour without a real tokenizer.
    """
    model = MagicMock()

    def _encode(sentences, **kwargs):  # noqa: ANN001, ANN202
        n = len(sentences) if isinstance(sentences, list) else 1
        rng = np.random.default_rng(42)
        return rng.random((n, embedding_dim)).astype(np.float32)

    model.encode.side_effect = _encode

    # Provide a minimal tokenizer mock.
    # encode(text, add_special_tokens=False) returns a list of int token IDs
    # whose length determines chunking behaviour.
    tokenizer = MagicMock()

    # Default: return a short token list (< _CHUNK_TOKENS) so the text is
    # treated as a single chunk.
    tokenizer.encode.return_value = list(range(50))  # 50 tokens → 1 chunk
    # decode returns a string representation of the chunk
    tokenizer.decode.side_effect = lambda ids, **kwargs: " ".join(str(i) for i in ids)

    model.tokenizer = tokenizer
    return model


def _make_embedder(model_mock: MagicMock | None = None) -> LyricEmbedder:
    """Construct a LyricEmbedder with a mock SentenceTransformer.

    The ``sentence_transformers.SentenceTransformer`` constructor is patched
    to return the provided (or freshly created) mock.
    """
    if model_mock is None:
        model_mock = _make_model_mock()

    import sentence_transformers  # resolves to stub

    sentence_transformers.SentenceTransformer.return_value = model_mock
    return LyricEmbedder(cache_dir=None)


# ---------------------------------------------------------------------------
# Tests: vector dimension
# ---------------------------------------------------------------------------


class TestLyricEmbedderDimension:
    """embed() must always return exactly 384 floats."""

    def test_embed_returns_list_of_384_floats(self) -> None:
        """Happy-path: a regular lyric string returns a 384-element list."""
        embedder = _make_embedder()
        result = embedder.embed("This is a test lyric line.\nAnother verse.")

        assert isinstance(result, list)
        assert len(result) == _EMBEDDING_DIM

    def test_embed_elements_are_floats(self) -> None:
        """Every element in the returned vector is a Python float."""
        embedder = _make_embedder()
        result = embedder.embed("Lyrics go here.")

        assert all(isinstance(v, float) for v in result)

    @pytest.mark.parametrize(
        "text",
        [
            "short",
            "A longer lyric with multiple words and sentences. " * 3,
            "こんにちは世界",   # Non-ASCII (Japanese)
            "привет мир",       # Non-ASCII (Russian)
        ],
    )
    def test_embed_dimension_for_various_texts(self, text: str) -> None:
        """Dimension is always 384 for any non-empty text input."""
        embedder = _make_embedder()
        result = embedder.embed(text)

        assert len(result) == _EMBEDDING_DIM


# ---------------------------------------------------------------------------
# Tests: empty / whitespace-only input returns zero vector
# ---------------------------------------------------------------------------


class TestLyricEmbedderZeroVectorCases:
    """Empty or whitespace-only text must return a zero vector of length 384."""

    def _assert_zero_vector(self, result: list[float]) -> None:
        assert len(result) == _EMBEDDING_DIM
        assert all(v == 0.0 for v in result), f"Expected zero vector, got {result[:5]}..."

    def test_empty_string_returns_zero_vector(self) -> None:
        """An empty string input returns a zero vector."""
        embedder = _make_embedder()
        result = embedder.embed("")

        self._assert_zero_vector(result)

    @pytest.mark.parametrize(
        "text",
        [
            " ",
            "   ",
            "\t",
            "\n",
            "\r\n",
            "  \t  \n  ",
        ],
    )
    def test_whitespace_only_returns_zero_vector(self, text: str) -> None:
        """Any whitespace-only input returns a zero vector."""
        embedder = _make_embedder()
        result = embedder.embed(text)

        self._assert_zero_vector(result)

    def test_encode_never_called_for_empty_input(self) -> None:
        """The model's encode() is never invoked for empty input."""
        model_mock = _make_model_mock()
        embedder = _make_embedder(model_mock)

        embedder.embed("")

        model_mock.encode.assert_not_called()

    def test_encode_never_called_for_whitespace_input(self) -> None:
        """The model's encode() is never invoked for whitespace-only input."""
        model_mock = _make_model_mock()
        embedder = _make_embedder(model_mock)

        embedder.embed("   \n  ")

        model_mock.encode.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: short text (single chunk)
# ---------------------------------------------------------------------------


class TestLyricEmbedderShortText:
    """Text that fits in one chunk is embedded with a single encode() call."""

    def test_short_text_calls_encode_once(self) -> None:
        """encode() is called exactly once when text fits in a single chunk."""
        model_mock = _make_model_mock()
        # tokenizer.encode returns < _CHUNK_TOKENS tokens → 1 chunk
        model_mock.tokenizer.encode.return_value = list(range(50))
        embedder = _make_embedder(model_mock)

        embedder.embed("A short lyric line.")

        model_mock.encode.assert_called_once()

    def test_short_text_encode_receives_original_text(self) -> None:
        """For short text, encode() is called with the original text (not chunk IDs)."""
        model_mock = _make_model_mock()
        model_mock.tokenizer.encode.return_value = list(range(50))  # short
        embedder = _make_embedder(model_mock)

        text = "A short lyric line."
        embedder.embed(text)

        call_args = model_mock.encode.call_args
        sentences_arg = call_args[0][0]  # positional arg 0
        assert sentences_arg == [text]

    def test_short_text_returns_384_floats(self) -> None:
        """Short text still returns exactly 384 floats."""
        embedder = _make_embedder()
        result = embedder.embed("short")

        assert len(result) == _EMBEDDING_DIM


# ---------------------------------------------------------------------------
# Tests: long text (multiple chunks) → averaged
# ---------------------------------------------------------------------------


class TestLyricEmbedderLongText:
    """Long text must be chunked; the resulting embeddings are averaged."""

    def _make_long_text_embedder(
        self, n_tokens: int = 600
    ) -> tuple["LyricEmbedder", MagicMock]:
        """Return an embedder whose tokenizer reports ``n_tokens`` for any text.

        The tokenizer mock produces ``n_tokens`` dummy token IDs so that
        ``_chunk_text`` generates multiple chunks.  Each chunk's ``decode``
        returns a unique non-empty string so that the chunk is not filtered out.
        """
        model_mock = _make_model_mock()

        # Override tokenizer to report n_tokens for any text
        model_mock.tokenizer.encode.return_value = list(range(n_tokens))

        # decode(ids) → non-empty decoded text per chunk
        model_mock.tokenizer.decode.side_effect = (
            lambda ids, **kwargs: f"chunk_text_{'_'.join(str(i) for i in ids[:3])}"
        )

        embedder = _make_embedder(model_mock)
        return embedder, model_mock

    def test_long_text_calls_encode_with_multiple_chunks(self) -> None:
        """For text > _CHUNK_TOKENS tokens, encode() receives multiple sentences."""
        n_tokens = 600  # 3 chunks of 256 (last chunk < 256)
        embedder, model_mock = self._make_long_text_embedder(n_tokens)

        embedder.embed("Long lyric text that exceeds the chunk limit.")

        call_args = model_mock.encode.call_args
        sentences_arg = call_args[0][0]
        # 600 tokens / 256 chunk size = 3 chunks (256, 256, 88)
        expected_chunks = (n_tokens + _CHUNK_TOKENS - 1) // _CHUNK_TOKENS
        assert len(sentences_arg) == expected_chunks

    def test_long_text_embedding_is_mean_of_chunk_embeddings(self) -> None:
        """The returned vector is the element-wise mean of chunk embeddings."""
        n_tokens = 512  # exactly 2 chunks of 256
        embedder, model_mock = self._make_long_text_embedder(n_tokens)

        # Make encode return deterministic per-chunk embeddings
        chunk_1 = np.array([1.0] * _EMBEDDING_DIM, dtype=np.float32)
        chunk_2 = np.array([3.0] * _EMBEDDING_DIM, dtype=np.float32)
        expected_mean = 2.0  # (1+3)/2

        model_mock.encode.side_effect = None
        model_mock.encode.return_value = np.array([chunk_1, chunk_2])  # (2, 384)

        result = embedder.embed("Some long lyric text.")

        assert len(result) == _EMBEDDING_DIM
        assert pytest.approx(result[0], abs=1e-5) == expected_mean

    def test_long_text_returns_384_floats(self) -> None:
        """Long text still returns exactly 384 floats."""
        embedder, _ = self._make_long_text_embedder(n_tokens=1000)

        result = embedder.embed("Very long lyrics that need chunking.")

        assert len(result) == _EMBEDDING_DIM

    def test_chunking_boundary_exact_multiple(self) -> None:
        """Chunking works correctly when token count is an exact multiple of chunk size."""
        n_tokens = _CHUNK_TOKENS * 3  # exactly 3 chunks
        embedder, model_mock = self._make_long_text_embedder(n_tokens)

        embedder.embed("Exactly three chunks of text.")

        call_args = model_mock.encode.call_args
        sentences_arg = call_args[0][0]
        assert len(sentences_arg) == 3

    def test_chunking_boundary_just_over(self) -> None:
        """One extra token beyond _CHUNK_TOKENS creates exactly 2 chunks."""
        n_tokens = _CHUNK_TOKENS + 1
        embedder, model_mock = self._make_long_text_embedder(n_tokens)

        embedder.embed("Just over one chunk of text.")

        call_args = model_mock.encode.call_args
        sentences_arg = call_args[0][0]
        assert len(sentences_arg) == 2


# ---------------------------------------------------------------------------
# Tests: synchronous API
# ---------------------------------------------------------------------------


class TestLyricEmbedderSync:
    """embed() must be synchronous — not a coroutine."""

    def test_embed_is_not_a_coroutine(self) -> None:
        """Calling embed() returns a list, not a coroutine object."""
        import asyncio

        embedder = _make_embedder()
        result = embedder.embed("A lyric line.")

        assert not asyncio.iscoroutine(result), (
            "embed() returned a coroutine — it must be synchronous"
        )

    def test_embed_does_not_require_event_loop(self) -> None:
        """embed() can be called outside of any async context."""
        embedder = _make_embedder()

        # If this raises RuntimeError("no running event loop"), the test fails.
        result = embedder.embed("Another lyric.")

        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Tests: encode failure fallback
# ---------------------------------------------------------------------------


class TestLyricEmbedderEncodeFallback:
    """If model.encode() raises, embed() must return a zero vector."""

    def test_encode_exception_returns_zero_vector(self) -> None:
        """Any exception from model.encode() causes embed() to return zeros."""
        model_mock = _make_model_mock()
        model_mock.encode.side_effect = RuntimeError("CUDA out of memory")
        embedder = _make_embedder(model_mock)

        result = embedder.embed("Some lyrics that fail to encode.")

        assert len(result) == _EMBEDDING_DIM
        assert all(v == 0.0 for v in result)
