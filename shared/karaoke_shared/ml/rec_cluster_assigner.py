"""Assign a track to the nearest rec-cluster based on pre-computed centroids.

Usage in worker pipeline:
    assigner = RecClusterAssigner("/data/models/rec_cluster_centroids.json")
    cluster_id = assigner.assign(audio_vector, lyrics_vector)
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import structlog

logger = structlog.get_logger(__name__)

_AUDIO_SCALE = math.sqrt(0.3 / 0.7)  # must match clustering script


class RecClusterAssigner:
    """Find the nearest rec-cluster for a new track."""

    def __init__(self, centroids_path: str | None = None) -> None:
        self._centroids: np.ndarray | None = None

        if centroids_path is None:
            return

        p = Path(centroids_path)
        if not p.exists():
            logger.warning("rec_cluster_centroids_not_found", path=centroids_path)
            return

        data = json.loads(p.read_text())
        self._centroids = np.array(data["centroids"], dtype=np.float32)
        logger.info(
            "rec_cluster_centroids_loaded",
            path=centroids_path,
            k=len(self._centroids),
        )

    @property
    def available(self) -> bool:
        return self._centroids is not None

    def assign(
        self,
        audio_vector: list[float] | None,
        lyrics_vector: list[float] | None,
    ) -> int | None:
        """Return the nearest cluster ID, or None if centroids not loaded."""
        if self._centroids is None:
            return None
        if audio_vector is None or lyrics_vector is None:
            return None

        # Build fused vector (same as clustering)
        audio = np.array(audio_vector, dtype=np.float32)
        lyrics = np.array(lyrics_vector, dtype=np.float32)

        audio_norm = np.linalg.norm(audio)
        if audio_norm > 1e-8:
            audio = audio / audio_norm
        audio = audio * _AUDIO_SCALE

        lyrics_norm = np.linalg.norm(lyrics)
        if lyrics_norm > 1e-8:
            lyrics = lyrics / lyrics_norm

        fused = np.concatenate([audio, lyrics])

        # Cosine similarity to each centroid
        centroid_norms = np.linalg.norm(self._centroids, axis=1, keepdims=True) + 1e-9
        normed_centroids = self._centroids / centroid_norms
        fused_norm = np.linalg.norm(fused) + 1e-9
        normed_fused = fused / fused_norm

        similarities = normed_centroids @ normed_fused
        best_cluster = int(np.argmax(similarities))

        return best_cluster
