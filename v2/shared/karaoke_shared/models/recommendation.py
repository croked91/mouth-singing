"""Pydantic models for the recommendation system."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class RecommendationStrategy(str, Enum):
    """Strategy used to compute the recommendation query vector."""

    POPULAR = "popular"
    LAST = "last"
    LAST_TWO_AVG = "last_two_avg"
    SESSION_AVG = "session_avg"


class RecommendedTrackItem(BaseModel):
    """Condensed track representation with similarity score."""

    id: str
    artist: str
    title: str
    duration_sec: int | None
    similarity_score: float


class RecommendationResponse(BaseModel):
    """Response returned by the recommendation endpoint."""

    strategy: RecommendationStrategy
    tracks: list[RecommendedTrackItem]
