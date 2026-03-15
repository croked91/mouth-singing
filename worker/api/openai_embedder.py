"""OpenAI Embeddings API client for lyric embedding.

Optional replacement for the local sentence-transformers LyricEmbedder.
Activated when LYRIC_EMBEDDER_BACKEND=openai.

WARNING: OpenAI text-embedding-3-small vectors are NOT compatible with
existing sentence-transformers vectors in QDrant. Only use for new
installations or after full reindexing.
"""

from __future__ import annotations

import httpx
import structlog

logger = structlog.get_logger(__name__)

_EMBEDDINGS_ENDPOINT = "https://api.openai.com/v1/embeddings"


class OpenAIEmbedderError(Exception):
    """Error from the OpenAI Embeddings API."""


class OpenAIEmbedder:
    """Synchronous OpenAI embeddings client.

    Designed to be called via ``asyncio.to_thread`` — matches the
    ``LyricEmbedder.embed()`` interface.

    Args:
        api_key: OpenAI API key.
        model: Embedding model name.
        dimensions: Output vector dimensionality.
        timeout: HTTP request timeout in seconds.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-3-small",
        dimensions: int = 384,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions
        self._timeout = timeout

    def embed(self, text: str) -> list[float]:
        """Embed text into a vector via the OpenAI API.

        Args:
            text: Input text (any language, any length).

        Returns:
            List of exactly ``dimensions`` floats. Returns a zero vector
            for empty input.

        Raises:
            OpenAIEmbedderError: On API errors.
        """
        stripped = text.strip()
        if not stripped:
            return [0.0] * self._dimensions

        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.post(
                    _EMBEDDINGS_ENDPOINT,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "input": stripped,
                        "model": self._model,
                        "dimensions": self._dimensions,
                    },
                )

            if resp.status_code >= 400:
                raise OpenAIEmbedderError(
                    f"Embeddings API error {resp.status_code}: {resp.text[:300]}"
                )

            data = resp.json()
            embedding = data["data"][0]["embedding"]

            usage = data.get("usage", {})
            total_tokens = usage.get("total_tokens", 0)
            logger.debug(
                "openai_embed_done",
                tokens=total_tokens,
                dimensions=len(embedding),
            )

            return embedding

        except OpenAIEmbedderError:
            raise
        except Exception as exc:
            logger.exception("openai_embed_failed")
            raise OpenAIEmbedderError(f"Embeddings request failed: {exc}") from exc
