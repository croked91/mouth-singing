"""Pydantic models for the job queue.

Schema reference:
- job_queue: id, track_id, priority, status, attempts, max_attempts,
  locked_by, locked_at, result (JSON), error_message, created_at, updated_at
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, Field


class Job(BaseModel):
    """Full job record, mirrors the job_queue table."""

    id: str
    track_id: str
    priority: int = 1
    status: str  # "pending" | "running" | "completed" | "failed"
    attempts: int = 0
    max_attempts: int = 3
    locked_by: str | None = None
    locked_at: str | None = None
    result: dict | None = None  # deserialized from JSON
    error_message: str | None = None
    created_at: str
    updated_at: str


class JobCreate(BaseModel):
    """Fields required to enqueue a new job."""

    track_id: str
    priority: int = 1
    # Server-side defaults
    id: str = Field(default_factory=lambda: str(uuid4()))
    status: str = "pending"
    attempts: int = 0
    max_attempts: int = 3
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
