"""Queue service — business logic for the karaoke play queue.

Wraps the SQLiteRepository so that route handlers stay thin. The
skip/finish flows that touch multiple tables all live here.
"""

from __future__ import annotations

import structlog
from karaoke_shared.constants import QueueEntryStatus
from karaoke_shared.models.play_history import PlayHistoryCreate
from karaoke_shared.models.queue import QueueEntry, QueueEntryCreate
from karaoke_shared.repositories.sqlite_repository import SQLiteRepository

logger = structlog.get_logger(__name__)


class QueueService:
    """Orchestrates queue entry lifecycle.

    Args:
        repo: An open SQLiteRepository for the current request.
    """

    def __init__(self, repo: SQLiteRepository) -> None:
        self.repo = repo

    async def get_queue(self, session_id: str) -> list[QueueEntry]:
        """Return active (queued + playing) entries for the session, by position."""
        return await self.repo.get_queue_by_session(session_id)

    async def add_to_queue(
        self, session_id: str, participant_id: str, track_id: str
    ) -> QueueEntry:
        """Append a new entry to the session queue."""
        return await self.repo.create_queue_entry(
            QueueEntryCreate(
                session_id=session_id,
                participant_id=participant_id,
                track_id=track_id,
            )
        )

    async def remove_from_queue(self, entry_id: str) -> None:
        """Permanently delete an entry from the queue."""
        await self.repo.delete_queue_entry(entry_id)

    async def skip_turn(self, entry_id: str) -> QueueEntry | None:
        """Move an entry to the end of the queue without losing its data.

        Marks the current entry as 'skipped' (preserving it for recommendation
        history) and creates a fresh entry at the end with the same participant
        and track. Returns the new entry, or ``None`` if *entry_id* was not found.
        """
        old_entry = await self.repo.get_queue_entry(entry_id)
        if old_entry is None:
            return None

        await self.repo.update_queue_entry_status(entry_id, QueueEntryStatus.SKIPPED)

        new_entry = await self.repo.create_queue_entry(
            QueueEntryCreate(
                session_id=old_entry.session_id,
                participant_id=old_entry.participant_id,
                track_id=old_entry.track_id,
            )
        )
        return new_entry

    async def get_current(self, session_id: str) -> QueueEntry | None:
        """Return the currently playing entry, or the next queued entry."""
        return await self.repo.get_current_entry(session_id)

    async def start_playing(self, entry_id: str) -> None:
        """Mark an entry as 'playing' (sets started_at timestamp)."""
        await self.repo.update_queue_entry_status(entry_id, QueueEntryStatus.PLAYING)

    async def finish_playing(self, entry_id: str) -> QueueEntry | None:
        """Complete playback for an entry and update all related counters.

        Steps performed:
        1. Mark the entry as 'done'.
        2. Write a play_history record.
        3. Increment the track's play_count.
        4. Increment the participant's tracks_played counter.
        5. Return the next queued/playing entry for this session (if any).

        Returns:
            The next QueueEntry, or ``None`` if the queue is now empty or
            *entry_id* was not found.
        """
        entry = await self.repo.get_queue_entry(entry_id)
        if entry is None:
            return None

        await self.repo.update_queue_entry_status(entry_id, QueueEntryStatus.DONE)

        await self.repo.create_play_history(
            PlayHistoryCreate(
                session_id=entry.session_id,
                participant_id=entry.participant_id,
                track_id=entry.track_id,
            )
        )

        await self.repo.increment_play_count(entry.track_id)
        await self.repo.increment_tracks_played(entry.participant_id)

        return await self.repo.get_current_entry(entry.session_id)
