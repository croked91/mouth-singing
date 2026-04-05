"""Pydantic models for the job queue.

Schema reference:
- job_queue: id, track_id (nullable), mp3_key, artist_hint, title_hint,
  priority, status, attempts, max_attempts, locked_by, locked_at,
  data (JSONB), result (JSONB), error_message, current_step, progress,
  created_at, updated_at
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, Field

from karaoke_shared.constants import JobStatus


class Job(BaseModel):
    """Full job record, mirrors the job_queue table."""

    id: str
    track_id: str | None = None
    mp3_key: str | None = None
    artist_hint: str | None = None
    title_hint: str | None = None
    priority: int = 1
    status: str
    attempts: int = 0
    max_attempts: int = 3
    locked_by: str | None = None
    locked_at: str | None = None
    data: dict | None = None  # intermediate pipeline data (JSONB)
    result: dict | None = None  # final result payload (JSONB)
    error_message: str | None = None
    current_step: str | None = None  # e.g. 'separating', 'transcribing'
    progress: int = 0  # 0-100
    created_at: str
    updated_at: str


class JobCreate(BaseModel):
    """Fields required to enqueue a new job."""

    track_id: str | None = None
    mp3_key: str | None = None
    artist_hint: str | None = None
    title_hint: str | None = None
    priority: int = 1
    # Server-side defaults
    id: str = Field(default_factory=lambda: str(uuid4()))
    status: str = JobStatus.PENDING
    attempts: int = 0
    max_attempts: int = 3
    data: dict | None = None
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class JobUpdate(BaseModel):
    """Partial update model for jobs — all fields are optional."""

    status: str | None = None
    attempts: int | None = None
    locked_by: str | None = None
    locked_at: str | None = None
    result: dict | None = None
    error_message: str | None = None
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
