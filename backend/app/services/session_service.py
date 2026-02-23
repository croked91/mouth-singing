"""Session service — business logic for sessions and participants.

Wraps the SQLiteRepository so that route handlers stay thin. All
nickname generation and uniqueness checks live here rather than in
the router.
"""

from karaoke_shared.models.session import (
    Participant,
    ParticipantCreate,
    Session,
    SessionCreate,
)
from karaoke_shared.repositories.sqlite_repository import SQLiteRepository

from app.utils.nicknames import generate_nickname


class SessionService:
    """Orchestrates session and participant operations.

    Args:
        repo: An open SQLiteRepository for the current request.
    """

    def __init__(self, repo: SQLiteRepository) -> None:
        self.repo = repo

    async def create_session(self, room_id: str) -> Session:
        """Create a new active session for the given room."""
        return await self.repo.create_session(SessionCreate(room_id=room_id))

    async def get_session(self, session_id: str) -> Session | None:
        """Return the session, or ``None`` if it does not exist."""
        return await self.repo.get_session(session_id)

    async def get_participants(self, session_id: str) -> list[Participant]:
        """Return all participants that belong to this session."""
        return await self.repo.get_participants_by_session(session_id)

    async def terminate_session(self, session_id: str) -> None:
        """Mark the session as terminated."""
        await self.repo.terminate_session(session_id)

    async def add_participant(
        self, session_id: str, name: str | None = None
    ) -> Participant:
        """Add a participant to the session, auto-generating a nickname if needed.

        If *name* is not provided (or is an empty string), a funny Russian
        nickname is generated that is unique within the current session.

        Args:
            session_id: The session to join.
            name: Optional display name chosen by the participant.

        Returns:
            The newly created Participant record.
        """
        if not name:
            existing = await self.repo.get_participants_by_session(session_id)
            existing_names = {p.display_name for p in existing}
            name = generate_nickname(existing_names)

        return await self.repo.create_participant(
            ParticipantCreate(session_id=session_id, display_name=name)
        )
