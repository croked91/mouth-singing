"""Pydantic models for catalog clusters (vibe-based groupings)."""

from __future__ import annotations

from pydantic import BaseModel


class CatalogCluster(BaseModel):
    """A vibe cluster computed from the catalog's audio+lyrics vectors."""

    id: int
    centroid_audio: list[float]
    centroid_lyrics: list[float]
    track_count: int
    created_at: str
    updated_at: str
