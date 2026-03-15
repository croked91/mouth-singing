"""Centralised constants for the karaoke application.

All magic strings (entity statuses, collection names, pipeline steps) are
defined here so that a typo becomes a NameError rather than a silent bug.
"""

from __future__ import annotations

from enum import StrEnum


# ------------------------------------------------------------------
# Entity statuses
# ------------------------------------------------------------------

class TrackStatus(StrEnum):
    """Lifecycle of a track from upload to playback-ready."""

    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    ERROR = "error"


class JobStatus(StrEnum):
    """Lifecycle of a processing job."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class QueueEntryStatus(StrEnum):
    """Status of a queue entry."""

    QUEUED = "queued"
    PLAYING = "playing"
    DONE = "done"
    SKIPPED = "skipped"


class SessionStatus(StrEnum):
    """Status of a karaoke session."""

    ACTIVE = "active"
    TERMINATED = "terminated"


class TrackSource(StrEnum):
    """How a track was added to the catalog."""

    CATALOG = "catalog"
    USER_UPLOAD = "user_upload"


# ------------------------------------------------------------------
# QDrant collection names
# ------------------------------------------------------------------

COLLECTION_AUDIO_FEATURES = "audio_features"
COLLECTION_LYRICS_EMBEDDINGS = "lyrics_embeddings"
COLLECTION_TRANSITIONS = "transitions"

# ------------------------------------------------------------------
# Vector dimensions
# ------------------------------------------------------------------

AUDIO_FEATURE_DIM = 45
LYRICS_EMBEDDING_DIM = 384
TRANSITION_DIM = AUDIO_FEATURE_DIM  # transitions use audio feature vectors

# ------------------------------------------------------------------
# Pipeline step names (used in job_queue.current_step)
# ------------------------------------------------------------------

class PipelineStep(StrEnum):
    """Named steps reported to the job_queue for SSE progress tracking."""

    SEPARATING = "separating"
    EXTRACTING_FEATURES = "extracting_features"
    VAD = "vad"
    TRANSCRIBING = "transcribing"
    SEARCHING_LYRICS = "searching_lyrics"
    ALIGNING = "aligning"
    LINE_BREAKING = "line_breaking"
    EMBEDDING_LYRICS = "embedding_lyrics"
    SYNCING_QDRANT = "syncing_qdrant"
    DONE = "done"
