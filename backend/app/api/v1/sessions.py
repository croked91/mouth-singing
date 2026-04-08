"""Sessions API router.

Endpoints:
    POST   /sessions                           Create a session
    GET    /sessions/{session_id}              Get session with participants
    POST   /sessions/{session_id}/participants Add a participant
    DELETE /sessions/{session_id}              Terminate session (admin only)
"""

from fastapi import APIRouter, Depends, Header, HTTPException, status
from karaoke_shared.models.session import Participant, Session
from karaoke_shared.repositories.pg_repository import PgRepository
from pydantic import BaseModel

from app.config import settings
from app.dependencies import get_repo
from app.services.session_service import SessionService

router = APIRouter()


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    """Request body for creating a new session."""

    room_id: str


class SessionDetail(BaseModel):
    """Session data with its participant list attached."""

    id: str
    room_id: str
    status: str
    created_at: str
    terminated_at: str | None = None
    participants: list[Participant]


class AddParticipantRequest(BaseModel):
    """Optional request body when adding a participant.

    If ``name`` is absent or empty the server auto-generates a nickname.
    """

    name: str | None = None


class HistoryItem(BaseModel):
    """A single played track in the session history."""

    track_id: str
    artist: str
    title: str
    duration_sec: int | None = None
    artist_image_url: str | None = None
    played_at: str
    source: str


class SessionHistoryResponse(BaseModel):
    """List of tracks played during a session."""

    items: list[HistoryItem]


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=Session,
    summary="Create a session",
)
async def create_session(
    body: CreateSessionRequest,
    repo: PgRepository = Depends(get_repo),
) -> Session:
    """Create a new active karaoke session for a room."""
    service = SessionService(repo)
    return await service.create_session(body.room_id)


@router.get(
    "/{session_id}",
    response_model=SessionDetail,
    summary="Get session with participants",
)
async def get_session(
    session_id: str,
    repo: PgRepository = Depends(get_repo),
) -> SessionDetail:
    """Return session data together with the full list of participants."""
    service = SessionService(repo)

    session = await service.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session '{session_id}' not found.",
        )

    participants = await service.get_participants(session_id)

    return SessionDetail(
        id=session.id,
        room_id=session.room_id,
        status=session.status,
        created_at=session.created_at,
        terminated_at=session.terminated_at,
        participants=participants,
    )


@router.post(
    "/{session_id}/participants",
    status_code=status.HTTP_201_CREATED,
    response_model=Participant,
    summary="Add a participant to a session",
)
async def add_participant(
    session_id: str,
    body: AddParticipantRequest,
    repo: PgRepository = Depends(get_repo),
) -> Participant:
    """Join a session as a participant.

    If ``name`` is omitted a funny Russian nickname is auto-generated that is
    unique within this session.
    """
    service = SessionService(repo)

    session = await service.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session '{session_id}' not found.",
        )

    if session.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Session '{session_id}' is not active.",
        )

    return await service.add_participant(session_id, body.name)


@router.get(
    "/{session_id}/history",
    response_model=SessionHistoryResponse,
    summary="Get tracks played during a session",
)
async def get_session_history(
    session_id: str,
    repo: PgRepository = Depends(get_repo),
) -> SessionHistoryResponse:
    """Return the list of tracks played in this session, newest first."""
    session = await repo.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session '{session_id}' not found.",
        )

    history = await repo.get_history_by_session(session_id)
    if not history:
        return SessionHistoryResponse(items=[])

    track_ids = list({h.track_id for h in history})
    tracks = await repo.get_tracks_by_ids(track_ids)

    artist_names = list({t.artist for t in tracks.values()})
    artists = await repo.get_artists_by_names(artist_names)

    items: list[HistoryItem] = []
    for h in sorted(history, key=lambda x: x.played_at, reverse=True):
        track = tracks.get(h.track_id)
        if track is None:
            continue
        artist_data = artists.get(track.artist)
        image_url = (
            f"/api/v1/media/artists/{artist_data['image_path']}"
            if artist_data and artist_data.get("image_path")
            else None
        )
        items.append(
            HistoryItem(
                track_id=track.id,
                artist=track.artist,
                title=track.title,
                duration_sec=track.duration_sec,
                artist_image_url=image_url,
                played_at=h.played_at,
                source=track.source,
            )
        )

    return SessionHistoryResponse(items=items)


@router.delete(
    "/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Terminate a session (admin)",
)
async def terminate_session(
    session_id: str,
    x_admin_secret: str | None = Header(default=None),
    repo: PgRepository = Depends(get_repo),
) -> None:
    """Terminate an active session.

    Requires the ``X-Admin-Secret`` header to match the configured
    ``admin_secret`` value.
    """
    import hmac

    if not hmac.compare_digest(x_admin_secret or "", settings.admin_secret):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing admin secret.",
        )

    service = SessionService(repo)

    session = await service.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session '{session_id}' not found.",
        )

    await service.terminate_session(session_id)
