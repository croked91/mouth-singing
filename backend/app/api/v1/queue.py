"""Queue API router.

Endpoints:
    GET    /sessions/{session_id}/queue  Get current + upcoming entries
    POST   /queue                        Add an entry to the queue
    POST   /queue/{entry_id}/skip        Move entry to end of queue
    POST   /queue/{entry_id}/start       Mark entry as playing
    POST   /queue/{entry_id}/finish      Complete playback
    DELETE /queue/{entry_id}             Remove entry from queue
"""

from fastapi import APIRouter, Depends, HTTPException, status
from karaoke_shared.models.queue import QueueEntry
from karaoke_shared.models.session import Participant
from karaoke_shared.models.track import SyllableTiming, Track
from karaoke_shared.repositories.sqlite_repository import SQLiteRepository
from pydantic import BaseModel

from app.dependencies import get_queue_service, get_sqlite_repo
from app.services.queue_service import QueueService

router = APIRouter()


# ---------------------------------------------------------------------------
# Response / request models
# ---------------------------------------------------------------------------


class QueueEntryWithDetails(BaseModel):
    """A queue entry enriched with participant and track information."""

    id: str
    session_id: str
    order_position: int
    status: str
    added_at: str
    started_at: str | None = None
    finished_at: str | None = None
    participant: Participant | None = None
    track: Track | None = None


class QueueResponse(BaseModel):
    """Full queue state for a session."""

    current: QueueEntryWithDetails | None
    upcoming: list[QueueEntryWithDetails]


class AddToQueueRequest(BaseModel):
    """Request body for adding an entry to the queue."""

    session_id: str
    participant_id: str
    track_id: str


class StartPlayingResponse(BaseModel):
    """Payload returned when playback starts — gives the frontend what it needs."""

    entry_id: str
    clip_url: str | None
    syllable_timings: list[SyllableTiming] | None
    duration_sec: int | None


class FinishPlayingResponse(BaseModel):
    """Payload returned after playback ends — points to the next performer."""

    next_participant: Participant | None
    next_entry_id: str | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enrich_entry(
    entry: QueueEntry,
    participants: dict,
    tracks: dict,
) -> QueueEntryWithDetails:
    """Attach participant and track data to a bare QueueEntry.

    Expects pre-loaded lookup dicts so that callers can batch-load all
    participants and tracks in a single query per table rather than issuing
    one query per entry (N+1 problem).
    """
    return QueueEntryWithDetails(
        id=entry.id,
        session_id=entry.session_id,
        order_position=entry.order_position,
        status=entry.status,
        added_at=entry.added_at,
        started_at=entry.started_at,
        finished_at=entry.finished_at,
        participant=participants.get(entry.participant_id),
        track=tracks.get(entry.track_id),
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.get(
    "/sessions/{session_id}/queue",
    response_model=QueueResponse,
    summary="Get the current and upcoming queue for a session",
)
async def get_queue(
    session_id: str,
    repo: SQLiteRepository = Depends(get_sqlite_repo),
) -> QueueResponse:
    """Return the full active queue split into 'current' and 'upcoming'.

    'current' is the entry that is playing (or the first queued entry if
    nothing is playing yet). 'upcoming' is everything else still in 'queued'
    status after the current entry.
    """
    service = QueueService(repo)
    entries = await service.get_queue(session_id)

    if not entries:
        return QueueResponse(current=None, upcoming=[])

    # Batch-load all participants and tracks referenced by the queue entries
    # in two queries rather than 2*N individual lookups.
    participant_ids = [e.participant_id for e in entries]
    track_ids = [e.track_id for e in entries]
    participants = await repo.get_participants_by_ids(participant_ids)
    tracks = await repo.get_tracks_by_ids(track_ids)

    # The first entry in the ordered list is the current one (playing or
    # first-queued). Everything after it is upcoming.
    current_entry = entries[0]
    upcoming_entries = entries[1:]

    current = _enrich_entry(current_entry, participants, tracks)
    upcoming = [_enrich_entry(e, participants, tracks) for e in upcoming_entries]

    return QueueResponse(current=current, upcoming=upcoming)


@router.post(
    "/queue",
    status_code=status.HTTP_201_CREATED,
    response_model=QueueEntry,
    summary="Add a track to the queue",
)
async def add_to_queue(
    body: AddToQueueRequest,
    repo: SQLiteRepository = Depends(get_sqlite_repo),
) -> QueueEntry:
    """Append a track to the session queue.

    Validates that the session exists and is active before inserting.
    """
    session = await repo.get_session(body.session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session '{body.session_id}' not found.",
        )
    if session.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Session '{body.session_id}' is not active.",
        )

    service = QueueService(repo)
    return await service.add_to_queue(
        body.session_id, body.participant_id, body.track_id
    )


@router.post(
    "/queue/{entry_id}/skip",
    response_model=QueueEntry,
    summary="Move a queue entry to the end",
)
async def skip_turn(
    entry_id: str,
    repo: SQLiteRepository = Depends(get_sqlite_repo),
) -> QueueEntry:
    """Skip the participant's current turn and re-queue them at the end.

    The original entry is marked 'skipped' (preserving recommendation data)
    and a new entry is created at the end of the queue.
    """
    service = QueueService(repo)
    new_entry = await service.skip_turn(entry_id)

    if new_entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Queue entry '{entry_id}' not found.",
        )

    return new_entry


@router.post(
    "/queue/{entry_id}/start",
    response_model=StartPlayingResponse,
    summary="Start playing a queue entry",
)
async def start_playing(
    entry_id: str,
    repo: SQLiteRepository = Depends(get_sqlite_repo),
) -> StartPlayingResponse:
    """Mark a queue entry as playing and return the track data needed by the player."""
    entry = await repo.get_queue_entry(entry_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Queue entry '{entry_id}' not found.",
        )

    service = QueueService(repo)
    await service.start_playing(entry_id)

    track = await repo.get_track(entry.track_id)

    stream_url = f"/api/v1/tracks/{entry.track_id}/stream" if track else None
    return StartPlayingResponse(
        entry_id=entry_id,
        clip_url=stream_url,
        syllable_timings=track.syllable_timings if track else None,
        duration_sec=track.duration_sec if track else None,
    )


@router.post(
    "/queue/{entry_id}/finish",
    response_model=FinishPlayingResponse,
    summary="Finish playing a queue entry",
)
async def finish_playing(
    entry_id: str,
    repo: SQLiteRepository = Depends(get_sqlite_repo),
    service: QueueService = Depends(get_queue_service),
) -> FinishPlayingResponse:
    """Mark a queue entry as done and advance to the next entry.

    Side effects:
    - Records play history.
    - Increments track play_count and participant tracks_played counters.
    - Updates participant portrait vector (recommendation system).
    - Records track transition for collaborative filtering.

    Returns the next participant and entry ID so the frontend can prepare
    the next song without polling.
    """
    # Verify the entry exists before handing off to the service.
    entry = await repo.get_queue_entry(entry_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Queue entry '{entry_id}' not found.",
        )

    next_entry = await service.finish_playing(entry_id)

    if next_entry is None:
        return FinishPlayingResponse(next_participant=None, next_entry_id=None)

    next_participant = await repo.get_participant(next_entry.participant_id)

    return FinishPlayingResponse(
        next_participant=next_participant,
        next_entry_id=next_entry.id,
    )


@router.delete(
    "/queue/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove an entry from the queue",
)
async def remove_from_queue(
    entry_id: str,
    repo: SQLiteRepository = Depends(get_sqlite_repo),
) -> None:
    """Permanently delete a queue entry.

    Used when a participant changes their mind before their turn starts.
    """
    entry = await repo.get_queue_entry(entry_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Queue entry '{entry_id}' not found.",
        )

    service = QueueService(repo)
    await service.remove_from_queue(entry_id)
