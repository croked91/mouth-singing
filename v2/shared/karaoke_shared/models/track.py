"""Pydantic models for tracks.

Schema reference:
- tracks: id, artist, title, duration_sec, mp3_path, instrumental_path,
  clip_path, lyrics_text, syllable_timings (JSON), language, source,
  status, error_message, play_count, qdrant_synced, created_at, updated_at
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, Field


class SyllableTiming(BaseModel):
    """One syllable with start/end timestamps (seconds from track start)."""

    syllable: str
    start: float
    end: float


class Track(BaseModel):
    """Full track record, mirrors the tracks table."""

    id: str
    artist: str
    title: str
    duration_sec: int | None = None
    mp3_path: str | None = None
    instrumental_path: str | None = None
    clip_path: str | None = None
    lyrics_text: str | None = None
    syllable_timings: list[SyllableTiming] | None = None
    language: str | None = None  # "ru" | "en" | "other"
    source: str  # "catalog" | "user_upload"
    status: str = "pending"  # "pending" | "processing" | "ready" | "error"
    error_message: str | None = None
    play_count: int = 0
    qdrant_synced: int = 0  # SQLite stores booleans as 0/1
    created_at: str
    updated_at: str


class TrackCreate(BaseModel):
    """Fields required (and optional) when creating a new track."""

    artist: str
    title: str
    source: str  # "catalog" | "user_upload"
    duration_sec: int | None = None
    mp3_path: str | None = None
    instrumental_path: str | None = None
    clip_path: str | None = None
    lyrics_text: str | None = None
    syllable_timings: list[SyllableTiming] | None = None
    language: str | None = None
    # Fields with server-side defaults
    id: str = Field(default_factory=lambda: str(uuid4()))
    status: str = "pending"
    play_count: int = 0
    qdrant_synced: int = 0
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class TrackUpdate(BaseModel):
    """Partial update model — all fields are optional.

    Only the fields that are explicitly set will be applied to the DB row.
    """

    artist: str | None = None
    title: str | None = None
    duration_sec: int | None = None
    mp3_path: str | None = None
    instrumental_path: str | None = None
    clip_path: str | None = None
    lyrics_text: str | None = None
    syllable_timings: list[SyllableTiming] | None = None
    language: str | None = None
    source: str | None = None
    status: str | None = None
    error_message: str | None = None
    play_count: int | None = None
    qdrant_synced: int | None = None
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
