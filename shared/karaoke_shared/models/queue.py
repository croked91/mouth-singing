"""Pydantic models for queue entries.

Schema reference:
- queue_entries: id, session_id, participant_id, track_id, order_position,
  status, added_at, started_at, finished_at
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, Field

from karaoke_shared.constants import QueueEntryStatus


class QueueEntry(BaseModel):
    """Full queue entry record, mirrors the queue_entries table."""

    id: str
    session_id: str
    participant_id: str
    track_id: str
    order_position: int
    status: str
    added_at: str
    started_at: str | None = None
    finished_at: str | None = None


class QueueEntryCreate(BaseModel):
    """Fields required to add a new entry to the queue.

    ``order_position`` is intentionally absent here — the repository
    assigns it automatically as ``max(order_position) + 1`` for the session.
    """

    session_id: str
    participant_id: str
    track_id: str
    # Server-side defaults
    id: str = Field(default_factory=lambda: str(uuid4()))
    status: str = QueueEntryStatus.QUEUED
    added_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
