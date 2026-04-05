"""Pydantic schemas for the rec-service HTTP API."""

from __future__ import annotations

from pydantic import BaseModel


class RecommendationRequest(BaseModel):
    played_track_ids: list[str]
    limit: int = 5
    language: str | None = None
    exclude_ids: list[str] | None = None


class TagRecommendationRequest(BaseModel):
    tag_id: int
    played_track_ids: list[str]
    limit: int = 5
    language: str | None = None


class TagsRequest(BaseModel):
    played_track_ids: list[str]
    limit: int = 8


class RecTrackItem(BaseModel):
    id: str
    artist: str
    title: str
    duration_sec: int | None = None
    similarity_score: float


class RecommendationResponse(BaseModel):
    strategy: str  # "cluster"
    tracks: list[RecTrackItem]
