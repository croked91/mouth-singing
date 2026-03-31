"""Lyric embedder using a multilingual sentence-transformers model.

Produces a 384-dimensional embedding vector from song lyrics.  Long texts are
split into overlapping chunks, embedded individually, and averaged so that the
full lyric content is represented regardless of token limit.

This module is synchronous. Call from async code via ``asyncio.to_thread``.
"""

from __future__ import annotations

import time

import structlog

logger = structlog.get_logger(__name__)

_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
_EMBEDDING_DIM = 384
# Conservative chunk size — model max is 512 tokens; 256 leaves headroom for
# the tokeniser's special tokens and avoids truncation on CJK-heavy text.
_CHUNK_TOKENS = 256


class LyricEmbedder:
    """Embeds song lyrics into a 384-dimensional vector.

    The sentence-transformers library is imported lazily in the constructor so
    that this module can be imported even when the package is not installed.

    Args:
        cache_dir: Optional directory for the Hugging Face model cache.
                   Passed directly to ``SentenceTransformer``.
    """

    def __init__(self, cache_dir: str | None = None) -> None:
        from sentence_transformers import SentenceTransformer  # lazy import

        self._model: SentenceTransformer = SentenceTransformer(
            _MODEL_NAME,
            cache_folder=cache_dir,
        )
        logger.info("lyric_embedder.model_loaded", model=_MODEL_NAME)

    def embed(self, text: str) -> list[float]:
        """Embed lyrics text into a 384-d vector.

        Long texts are split into chunks of ~256 tokens, embedded separately,
        and averaged.  The result is *not* L2-normalised — raw mean-pooled
        embeddings are returned, consistent with sentence-transformers
        defaults.

        Args:
            text: Raw lyric text (any language, any length).

        Returns:
            List of exactly 384 floats.  Returns a zero vector for empty input.
        """
        import numpy as np

        logger.info("lyric_embedding_starting")
        t0 = time.monotonic()

        stripped = text.strip()
        if not stripped:
            return [0.0] * _EMBEDDING_DIM

        chunks = self._chunk_text(stripped)
        if not chunks:
            return [0.0] * _EMBEDDING_DIM

        try:
            embeddings = self._model.encode(
                chunks,
                batch_size=32,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
        except Exception:
            logger.exception("lyric_embedder.encode_failed")
            return [0.0] * _EMBEDDING_DIM

        mean_vec: np.ndarray = embeddings.mean(axis=0)

        logger.info(
            "lyric_embedding_completed",
            chunks=len(chunks),
            duration_sec=round(time.monotonic() - t0, 2),
        )

        return mean_vec.tolist()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _chunk_text(self, text: str) -> list[str]:
        """Split text into token-budget chunks using the model's tokeniser.

        The tokeniser converts text to token IDs; chunks are formed by slicing
        token sequences so no chunk exceeds ``_CHUNK_TOKENS`` tokens.  Chunks
        are decoded back to strings before embedding.

        Args:
            text: Non-empty lyric string.

        Returns:
            List of one or more text chunks ready to embed.
        """
        tokenizer = self._model.tokenizer
        token_ids: list[int] = tokenizer.encode(text, add_special_tokens=False)

        if len(token_ids) <= _CHUNK_TOKENS:
            return [text]

        chunks: list[str] = []
        for start in range(0, len(token_ids), _CHUNK_TOKENS):
            chunk_ids = token_ids[start : start + _CHUNK_TOKENS]
            chunk_text = tokenizer.decode(chunk_ids, skip_special_tokens=True).strip()
            if chunk_text:
                chunks.append(chunk_text)

        return chunks if chunks else [text]
