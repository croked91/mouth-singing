"""Pydantic models for mood tags."""

from __future__ import annotations

from pydantic import BaseModel


class MoodTag(BaseModel):
    """A mood tag linked to a catalog cluster."""

    id: int
    name: str
    cluster_id: int
    created_at: str


class MoodTagResponse(BaseModel):
    """API response item for a mood tag."""

    id: int
    name: str
