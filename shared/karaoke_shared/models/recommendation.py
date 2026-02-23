"""Pydantic models for the recommendation system."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from karaoke_shared.models.track import Track


class RecommendationStrategy(str, Enum):
    """Strategy used to compute the recommendation query vector."""

    POPULAR = "popular"
    LAST = "last"
    LAST_TWO_AVG = "last_two_avg"
    SESSION_AVG = "session_avg"


class RecommendationResponse(BaseModel):
    """Response returned by the recommendation endpoint."""

    tracks: list[Track]
    strategy: RecommendationStrategy
