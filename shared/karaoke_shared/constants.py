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


class PopularityCategory(StrEnum):
    """Track popularity tier for recommendation re-ranking."""

    ETERNAL_HIT = "eternal_hit"
    CURRENT_HIT = "current_hit"
    FORMER_HIT = "former_hit"
    ARTIST_BEST = "artist_best"
    REGULAR = "regular"


# Categories considered "well-known" for recommendation filtering.
WELL_KNOWN_CATEGORIES: list[str] = [
    PopularityCategory.ETERNAL_HIT,
    PopularityCategory.CURRENT_HIT,
    PopularityCategory.ARTIST_BEST,
    PopularityCategory.FORMER_HIT,
]


# ------------------------------------------------------------------
# QDrant collection names
# ------------------------------------------------------------------

COLLECTION_AUDIO_FEATURES = "audio_features"
COLLECTION_LYRICS_EMBEDDINGS = "lyrics_embeddings"

# ------------------------------------------------------------------
# Vector dimensions
# ------------------------------------------------------------------

AUDIO_FEATURE_DIM = 45
LYRICS_EMBEDDING_DIM = 384

# ------------------------------------------------------------------
# Pipeline step names (used in job_queue.current_step)
# ------------------------------------------------------------------

class PipelineStep(StrEnum):
    """Named steps reported to the job_queue for SSE progress tracking.

    Feature extraction, lyric embedding, and QDrant sync have moved to the
    Rec Service and are no longer part of the worker pipeline.
    """

    SEPARATING = "separating"
    VAD = "vad"
    TRANSCRIBING = "transcribing"
    SEARCHING_LYRICS = "searching_lyrics"
    ALIGNING = "aligning"
    LINE_BREAKING = "line_breaking"
