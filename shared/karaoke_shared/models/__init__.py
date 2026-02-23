"""Domain models for the karaoke application.

All Pydantic models are re-exported from this package so callers can do:

    from karaoke_shared.models import Track, TrackCreate, Session, ...
"""

from karaoke_shared.models.job import Job, JobCreate, JobUpdate
from karaoke_shared.models.play_history import PlayHistoryCreate, PlayHistoryEntry
from karaoke_shared.models.queue import QueueEntry, QueueEntryCreate
from karaoke_shared.models.recommendation import (
    RecommendationResponse,
    RecommendationStrategy,
)
from karaoke_shared.models.session import (
    Participant,
    ParticipantCreate,
    Session,
    SessionCreate,
)
from karaoke_shared.models.track import (
    SyllableTiming,
    Track,
    TrackCreate,
    TrackUpdate,
)

__all__ = [
    # job
    "Job",
    "JobCreate",
    "JobUpdate",
    # play_history
    "PlayHistoryCreate",
    "PlayHistoryEntry",
    # queue
    "QueueEntry",
    "QueueEntryCreate",
    # recommendation
    "RecommendationResponse",
    "RecommendationStrategy",
    # session
    "Participant",
    "ParticipantCreate",
    "Session",
    "SessionCreate",
    # track
    "SyllableTiming",
    "Track",
    "TrackCreate",
    "TrackUpdate",
]
