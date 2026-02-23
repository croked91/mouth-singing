"""Sentence-transformers embedder for semantic search.

This module is loaded lazily and optionally. If sentence-transformers is not
installed or the model fails to load, the caller receives None instead of an
Embedder instance, and the search service falls back to FTS-only mode.

Usage::

    from app.services.embedder import Embedder

    embedder = Embedder()
    vector = embedder.embed("Bohemian Rhapsody Queen")
"""

from __future__ import annotations


class Embedder:
    """Wraps a sentence-transformers model for query-to-vector encoding.

    The model is loaded eagerly on construction. If the import or download
    fails, the constructor raises an exception — the caller is responsible
    for catching it and treating the embedder as unavailable.
    """

    MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

    def __init__(self) -> None:
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415

        self.model = SentenceTransformer(self.MODEL_NAME)

    def embed(self, text: str) -> list[float]:
        """Encode *text* into a dense vector.

        Args:
            text: The query or document text to embed.

        Returns:
            A list of floats representing the embedding.
        """
        return self.model.encode(text).tolist()
