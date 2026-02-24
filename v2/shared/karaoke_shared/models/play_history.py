"""Pydantic models for play history.

Schema reference:
- play_history: id, session_id, participant_id, track_id, played_at, completed
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, Field


class PlayHistoryEntry(BaseModel):
    """Full play history record, mirrors the play_history table."""

    id: str
    session_id: str
    participant_id: str
    track_id: str
    played_at: str
    completed: int = 0  # SQLite stores booleans as 0/1


class PlayHistoryCreate(BaseModel):
    """Fields required to create a new play history entry."""

    session_id: str
    participant_id: str
    track_id: str
    # Server-side defaults
    id: str = Field(default_factory=lambda: str(uuid4()))
    completed: int = 0
    played_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
