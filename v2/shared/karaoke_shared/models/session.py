"""Pydantic models for sessions and participants.

Schema reference:
- sessions: id, room_id, status, created_at, terminated_at
- participants: id, session_id, display_name, portrait_vector, tracks_played, created_at
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, Field


class Session(BaseModel):
    """Full session record, mirrors the sessions table."""

    id: str
    room_id: str
    status: str  # "active" | "terminated"
    created_at: str
    terminated_at: str | None = None


class SessionCreate(BaseModel):
    """Fields required to create a new session."""

    room_id: str
    id: str = Field(default_factory=lambda: str(uuid4()))
    status: str = "active"
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class Participant(BaseModel):
    """Full participant record, mirrors the participants table."""

    id: str
    session_id: str
    display_name: str
    portrait_vector: list[float] | None = None
    lyrics_portrait_vector: list[float] | None = None
    tracks_played: int = 0
    created_at: str


class ParticipantCreate(BaseModel):
    """Fields required to create a new participant."""

    session_id: str
    display_name: str
    id: str = Field(default_factory=lambda: str(uuid4()))
    tracks_played: int = 0
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
