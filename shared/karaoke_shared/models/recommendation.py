"""Pydantic models for the recommendation system."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class RecommendationStrategy(str, Enum):
    """Strategy used to compute the recommendation list."""

    POPULAR = "popular"
    CLUSTER = "cluster"


class RecommendedTrackItem(BaseModel):
    """Condensed track representation with similarity score."""

    id: str
    artist: str
    title: str
    duration_sec: int | None
    similarity_score: float
    artist_image_url: str | None = None


class RecommendationResponse(BaseModel):
    """Response returned by the recommendation endpoint."""

    strategy: RecommendationStrategy
    tracks: list[RecommendedTrackItem]
