"""Async SQLite repository.

All database I/O goes through an injected ``aiosqlite.Connection``.
The connection is opened once at application startup (lifespan) and shared
across requests via FastAPI's ``app.state.db``.

Usage::

    repo = SQLiteRepository(db)
    track = await repo.create_track(
        TrackCreate(artist="...", title="...", source="catalog")
    )
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from karaoke_shared.models.job import Job, JobCreate
from karaoke_shared.models.play_history import PlayHistoryCreate, PlayHistoryEntry
from karaoke_shared.models.queue import QueueEntry, QueueEntryCreate
from karaoke_shared.models.session import (
    Participant,
    ParticipantCreate,
    Session,
    SessionCreate,
)
from karaoke_shared.models.track import SyllableTiming, Track, TrackCreate, TrackUpdate


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


class SQLiteRepository:
    """Async repository backed by a shared ``aiosqlite.Connection``.

    The connection is injected rather than created here so that all requests
    within the same process share one connection (and thus one WAL file).
    """

    def __init__(self, db: aiosqlite.Connection) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _row_to_dict(self, row: aiosqlite.Row) -> dict[str, Any]:
        """Convert an aiosqlite.Row to a plain Python dict."""
        return dict(row)

    # ------------------------------------------------------------------
    # Tracks
    # ------------------------------------------------------------------

    async def create_track(self, data: TrackCreate) -> Track:
        """Insert a new track row and return the full Track model."""
        syllable_timings_json: str | None = None
        if data.syllable_timings is not None:
            syllable_timings_json = json.dumps(
                [st.model_dump() for st in data.syllable_timings]
            )

        await self.db.execute(
            """
            INSERT INTO tracks (
                id, artist, title, duration_sec, mp3_path, instrumental_path,
                clip_path, lyrics_text, syllable_timings, language, source,
                status, error_message, play_count, qdrant_synced,
                created_at, updated_at
            ) VALUES (
                :id, :artist, :title, :duration_sec, :mp3_path, :instrumental_path,
                :clip_path, :lyrics_text, :syllable_timings, :language, :source,
                :status, :error_message, :play_count, :qdrant_synced,
                :created_at, :updated_at
            )
            """,
            {
                "id": data.id,
                "artist": data.artist,
                "title": data.title,
                "duration_sec": data.duration_sec,
                "mp3_path": data.mp3_path,
                "instrumental_path": data.instrumental_path,
                "clip_path": data.clip_path,
                "lyrics_text": data.lyrics_text,
                "syllable_timings": syllable_timings_json,
                "language": data.language,
                "source": data.source,
                "status": data.status,
                "error_message": None,
                "play_count": data.play_count,
                "qdrant_synced": data.qdrant_synced,
                "created_at": data.created_at,
                "updated_at": data.updated_at,
            },
        )
        await self.db.commit()

        track = await self.get_track(data.id)
        if track is None:
            raise RuntimeError(f"Track {data.id} not found after insert")
        return track

    async def get_track(self, track_id: str) -> Track | None:
        """Return a single track by primary key, or ``None`` if not found."""
        cursor = await self.db.execute(
            "SELECT * FROM tracks WHERE id = ?", (track_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._track_from_row(row)

    async def update_track(self, track_id: str, data: TrackUpdate) -> Track | None:
        """Apply a partial update to a track and return the updated model.

        Only fields that are not ``None`` in *data* are included in the SET
        clause, plus ``updated_at`` which is always refreshed.
        """
        updates: dict[str, Any] = {"updated_at": data.updated_at}

        for field in (
            "artist",
            "title",
            "duration_sec",
            "mp3_path",
            "instrumental_path",
            "clip_path",
            "lyrics_text",
            "language",
            "source",
            "status",
            "error_message",
            "play_count",
            "qdrant_synced",
        ):
            value = getattr(data, field)
            if value is not None:
                updates[field] = value

        if data.syllable_timings is not None:
            updates["syllable_timings"] = json.dumps(
                [st.model_dump() for st in data.syllable_timings]
            )

        set_clause = ", ".join(f"{col} = :{col}" for col in updates)
        updates["track_id"] = track_id

        await self.db.execute(
            f"UPDATE tracks SET {set_clause} WHERE id = :track_id",  # noqa: S608
            updates,
        )
        await self.db.commit()

        return await self.get_track(track_id)

    async def list_popular(self, limit: int = 10) -> list[Track]:
        """Return the most-played ready tracks, ordered by play count descending."""
        cursor = await self.db.execute(
            "SELECT * FROM tracks"
            " WHERE status = 'ready' ORDER BY play_count DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [self._track_from_row(row) for row in rows]

    async def search_fts(
        self, query: str, limit: int = 20, offset: int = 0
    ) -> list[Track]:
        """Full-text search over artist, title, and lyrics using FTS5.

        Results are joined back to the tracks table so we get full row data.
        Only tracks with status='ready' are returned.

        Returns an empty list if the FTS5 query syntax is invalid.
        """
        try:
            cursor = await self.db.execute(
                """
                SELECT t.*
                FROM tracks_fts fts
                JOIN tracks t ON t.rowid = fts.rowid
                WHERE tracks_fts MATCH ?
                  AND t.status = 'ready'
                ORDER BY rank
                LIMIT ? OFFSET ?
                """,
                (query, limit, offset),
            )
        except Exception:
            # FTS5 MATCH has its own query syntax; malformed user input
            # (unbalanced quotes, trailing operators) causes OperationalError.
            return []
        rows = await cursor.fetchall()
        return [self._track_from_row(row) for row in rows]

    async def increment_play_count(self, track_id: str) -> None:
        """Increment the play_count counter for a track by 1."""
        await self.db.execute(
            "UPDATE tracks"
            " SET play_count = play_count + 1, updated_at = ? WHERE id = ?",
            (_now_iso(), track_id),
        )
        await self.db.commit()

    async def suggest_tracks(
        self, query: str, limit: int = 10
    ) -> list[dict[str, str]]:
        """Prefix search on artist and title for autocomplete.

        Returns a list of dicts with ``'artist'`` and ``'title'`` keys.
        Only tracks with ``status='ready'`` are considered.

        Args:
            query: The prefix string to match (case-insensitive LIKE).
            limit: Maximum number of results to return.

        Returns:
            A list of ``{'artist': str, 'title': str}`` dicts.
        """
        pattern = f"{query}%"
        cursor = await self.db.execute(
            """
            SELECT DISTINCT artist, title FROM tracks
            WHERE (artist LIKE ? OR title LIKE ?)
              AND status = 'ready'
            LIMIT ?
            """,
            (pattern, pattern, limit),
        )
        rows = await cursor.fetchall()
        return [{"artist": row[0], "title": row[1]} for row in rows]

    def _track_from_row(self, row: aiosqlite.Row) -> Track:
        """Build a Track model from a DB row, deserializing JSON fields."""
        data = self._row_to_dict(row)

        syllable_timings: list[SyllableTiming] | None = None
        raw_timings = data.get("syllable_timings")
        if isinstance(raw_timings, str):
            parsed = json.loads(raw_timings)
            syllable_timings = [SyllableTiming(**item) for item in parsed]

        return Track(
            id=data["id"],
            artist=data["artist"],
            title=data["title"],
            duration_sec=data.get("duration_sec"),
            mp3_path=data.get("mp3_path"),
            instrumental_path=data.get("instrumental_path"),
            clip_path=data.get("clip_path"),
            lyrics_text=data.get("lyrics_text"),
            syllable_timings=syllable_timings,
            language=data.get("language"),
            source=data["source"],
            status=data["status"],
            error_message=data.get("error_message"),
            play_count=data.get("play_count", 0),
            qdrant_synced=data.get("qdrant_synced", 0),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    async def create_session(self, data: SessionCreate) -> Session:
        """Insert a new session row and return the Session model."""
        await self.db.execute(
            """
            INSERT INTO sessions (id, room_id, status, created_at, terminated_at)
            VALUES (:id, :room_id, :status, :created_at, :terminated_at)
            """,
            {
                "id": data.id,
                "room_id": data.room_id,
                "status": data.status,
                "created_at": data.created_at,
                "terminated_at": None,
            },
        )
        await self.db.commit()

        session = await self.get_session(data.id)
        if session is None:
            raise RuntimeError(f"Session {data.id} not found after insert")
        return session

    async def get_session(self, session_id: str) -> Session | None:
        """Return a single session by primary key, or ``None`` if not found."""
        cursor = await self.db.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._session_from_row(row)

    async def terminate_session(self, session_id: str) -> None:
        """Mark a session as terminated and record the termination timestamp."""
        await self.db.execute(
            "UPDATE sessions SET status = 'terminated', terminated_at = ? WHERE id = ?",
            (_now_iso(), session_id),
        )
        await self.db.commit()

    async def get_active_by_room(self, room_id: str) -> Session | None:
        """Return the active session for a room, or ``None`` if none exists."""
        cursor = await self.db.execute(
            "SELECT * FROM sessions WHERE room_id = ? AND status = 'active' LIMIT 1",
            (room_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._session_from_row(row)

    def _session_from_row(self, row: aiosqlite.Row) -> Session:
        data = self._row_to_dict(row)
        return Session(
            id=data["id"],
            room_id=data["room_id"],
            status=data["status"],
            created_at=data["created_at"],
            terminated_at=data.get("terminated_at"),
        )

    # ------------------------------------------------------------------
    # Participants
    # ------------------------------------------------------------------

    async def create_participant(self, data: ParticipantCreate) -> Participant:
        """Insert a new participant row and return the Participant model."""
        await self.db.execute(
            """
            INSERT INTO participants (
                id, session_id, display_name, portrait_vector, tracks_played, created_at
            ) VALUES (
                :id, :session_id, :display_name, :portrait_vector,
                :tracks_played, :created_at
            )
            """,
            {
                "id": data.id,
                "session_id": data.session_id,
                "display_name": data.display_name,
                "portrait_vector": None,
                "tracks_played": data.tracks_played,
                "created_at": data.created_at,
            },
        )
        await self.db.commit()

        participant = await self.get_participant(data.id)
        if participant is None:
            raise RuntimeError(f"Participant {data.id} not found after insert")
        return participant

    async def get_participants_by_session(self, session_id: str) -> list[Participant]:
        """Return all participants belonging to a session."""
        cursor = await self.db.execute(
            "SELECT * FROM participants WHERE session_id = ?",
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [self._participant_from_row(row) for row in rows]

    async def get_participant(self, participant_id: str) -> Participant | None:
        """Return a single participant by primary key, or ``None`` if not found."""
        cursor = await self.db.execute(
            "SELECT * FROM participants WHERE id = ?", (participant_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._participant_from_row(row)

    async def update_portrait(
        self, participant_id: str, portrait_vector: list[float]
    ) -> None:
        """Persist the participant's interest portrait vector as JSON."""
        await self.db.execute(
            "UPDATE participants SET portrait_vector = ? WHERE id = ?",
            (json.dumps(portrait_vector), participant_id),
        )
        await self.db.commit()

    async def increment_tracks_played(self, participant_id: str) -> None:
        """Increment the tracks_played counter for a participant by 1."""
        await self.db.execute(
            "UPDATE participants SET tracks_played = tracks_played + 1 WHERE id = ?",
            (participant_id,),
        )
        await self.db.commit()

    def _participant_from_row(self, row: aiosqlite.Row) -> Participant:
        data = self._row_to_dict(row)

        portrait_vector: list[float] | None = None
        raw_portrait = data.get("portrait_vector")
        if isinstance(raw_portrait, str):
            portrait_vector = json.loads(raw_portrait)

        return Participant(
            id=data["id"],
            session_id=data["session_id"],
            display_name=data["display_name"],
            portrait_vector=portrait_vector,
            tracks_played=data.get("tracks_played", 0),
            created_at=data["created_at"],
        )

    # ------------------------------------------------------------------
    # Queue entries
    # ------------------------------------------------------------------

    async def create_queue_entry(self, data: QueueEntryCreate) -> QueueEntry:
        """Add an entry to the queue and return the full QueueEntry model.

        The ``order_position`` is assigned atomically via a subquery so that
        concurrent inserts within the same session cannot produce duplicates.
        """
        await self.db.execute(
            """
            INSERT INTO queue_entries (
                id, session_id, participant_id, track_id,
                order_position, status, added_at, started_at, finished_at
            ) VALUES (
                :id, :session_id, :participant_id, :track_id,
                (SELECT COALESCE(MAX(order_position), 0) + 1
                 FROM queue_entries WHERE session_id = :session_id),
                :status, :added_at, :started_at, :finished_at
            )
            """,
            {
                "id": data.id,
                "session_id": data.session_id,
                "participant_id": data.participant_id,
                "track_id": data.track_id,
                "status": data.status,
                "added_at": data.added_at,
                "started_at": None,
                "finished_at": None,
            },
        )
        await self.db.commit()

        entry = await self._get_queue_entry(data.id)
        if entry is None:
            raise RuntimeError(f"QueueEntry {data.id} not found after insert")
        return entry

    async def get_queue_by_session(self, session_id: str) -> list[QueueEntry]:
        """Return queued and playing entries for a session, ordered by position."""
        cursor = await self.db.execute(
            """
            SELECT * FROM queue_entries
            WHERE session_id = ?
              AND status IN ('queued', 'playing')
            ORDER BY order_position
            """,
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [self._queue_entry_from_row(row) for row in rows]

    async def update_queue_entry_status(self, entry_id: str, status: str) -> None:
        """Update the status of a queue entry.

        Side effects:
        - When transitioning to ``'playing'``, ``started_at`` is set to now.
        - When transitioning to ``'done'`` or ``'skipped'``,
          ``finished_at`` is set to now.
        """
        now = _now_iso()

        if status == "playing":
            await self.db.execute(
                "UPDATE queue_entries SET status = ?, started_at = ? WHERE id = ?",
                (status, now, entry_id),
            )
        elif status in ("done", "skipped"):
            await self.db.execute(
                "UPDATE queue_entries SET status = ?, finished_at = ? WHERE id = ?",
                (status, now, entry_id),
            )
        else:
            await self.db.execute(
                "UPDATE queue_entries SET status = ? WHERE id = ?",
                (status, entry_id),
            )

        await self.db.commit()

    async def delete_queue_entry(self, entry_id: str) -> None:
        """Permanently remove a queue entry."""
        await self.db.execute(
            "DELETE FROM queue_entries WHERE id = ?", (entry_id,)
        )
        await self.db.commit()

    async def get_current_entry(self, session_id: str) -> QueueEntry | None:
        """Return the currently playing entry, or the next queued entry.

        Preference order: status='playing' first, then the lowest-position
        'queued' entry.
        """
        cursor = await self.db.execute(
            """
            SELECT * FROM queue_entries
            WHERE session_id = ?
              AND status IN ('playing', 'queued')
            ORDER BY
                CASE status WHEN 'playing' THEN 0 ELSE 1 END,
                order_position
            LIMIT 1
            """,
            (session_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._queue_entry_from_row(row)

    async def _get_queue_entry(self, entry_id: str) -> QueueEntry | None:
        """Fetch a single queue entry by primary key."""
        cursor = await self.db.execute(
            "SELECT * FROM queue_entries WHERE id = ?", (entry_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._queue_entry_from_row(row)

    def _queue_entry_from_row(self, row: aiosqlite.Row) -> QueueEntry:
        data = self._row_to_dict(row)
        return QueueEntry(
            id=data["id"],
            session_id=data["session_id"],
            participant_id=data["participant_id"],
            track_id=data["track_id"],
            order_position=data["order_position"],
            status=data["status"],
            added_at=data["added_at"],
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
        )

    # ------------------------------------------------------------------
    # Play history
    # ------------------------------------------------------------------

    async def create_play_history(self, data: PlayHistoryCreate) -> PlayHistoryEntry:
        """Insert a play history record and return the full model."""
        await self.db.execute(
            """
            INSERT INTO play_history (
                id, session_id, participant_id, track_id, played_at, completed
            ) VALUES (
                :id, :session_id, :participant_id, :track_id, :played_at, :completed
            )
            """,
            {
                "id": data.id,
                "session_id": data.session_id,
                "participant_id": data.participant_id,
                "track_id": data.track_id,
                "played_at": data.played_at,
                "completed": data.completed,
            },
        )
        await self.db.commit()

        cursor = await self.db.execute(
            "SELECT * FROM play_history WHERE id = ?", (data.id,)
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError(f"PlayHistoryEntry {data.id} not found after insert")
        return self._play_history_from_row(row)

    async def get_history_by_participant(
        self, participant_id: str, limit: int = 20
    ) -> list[PlayHistoryEntry]:
        """Return the most recent play history entries for a participant."""
        cursor = await self.db.execute(
            """
            SELECT * FROM play_history
            WHERE participant_id = ?
            ORDER BY played_at DESC
            LIMIT ?
            """,
            (participant_id, limit),
        )
        rows = await cursor.fetchall()
        return [self._play_history_from_row(row) for row in rows]

    async def get_history_by_session(self, session_id: str) -> list[PlayHistoryEntry]:
        """Return all play history entries for a session."""
        cursor = await self.db.execute(
            "SELECT * FROM play_history WHERE session_id = ?",
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [self._play_history_from_row(row) for row in rows]

    def _play_history_from_row(self, row: aiosqlite.Row) -> PlayHistoryEntry:
        data = self._row_to_dict(row)
        return PlayHistoryEntry(
            id=data["id"],
            session_id=data["session_id"],
            participant_id=data["participant_id"],
            track_id=data["track_id"],
            played_at=data["played_at"],
            completed=data.get("completed", 0),
        )

    # ------------------------------------------------------------------
    # Job queue
    # ------------------------------------------------------------------

    async def create_job(self, data: JobCreate) -> Job:
        """Enqueue a new processing job and return the Job model."""
        await self.db.execute(
            """
            INSERT INTO job_queue (
                id, track_id, priority, status, attempts, max_attempts,
                locked_by, locked_at, result, error_message, created_at, updated_at
            ) VALUES (
                :id, :track_id, :priority, :status, :attempts, :max_attempts,
                :locked_by, :locked_at, :result, :error_message,
                :created_at, :updated_at
            )
            """,
            {
                "id": data.id,
                "track_id": data.track_id,
                "priority": data.priority,
                "status": data.status,
                "attempts": data.attempts,
                "max_attempts": data.max_attempts,
                "locked_by": None,
                "locked_at": None,
                "result": None,
                "error_message": None,
                "created_at": data.created_at,
                "updated_at": data.updated_at,
            },
        )
        await self.db.commit()

        job = await self.get_job(data.id)
        if job is None:
            raise RuntimeError(f"Job {data.id} not found after insert")
        return job

    async def get_job(self, job_id: str) -> Job | None:
        """Return a single job by primary key, or ``None`` if not found."""
        cursor = await self.db.execute(
            "SELECT * FROM job_queue WHERE id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._job_from_row(row)

    async def poll_pending(self, limit: int = 1) -> list[Job]:
        """Return the highest-priority pending jobs without locking them.

        Use :meth:`lock_job` to claim a job before processing it.
        """
        cursor = await self.db.execute(
            """
            SELECT * FROM job_queue
            WHERE status = 'pending'
            ORDER BY priority DESC, created_at ASC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [self._job_from_row(row) for row in rows]

    async def lock_job(self, job_id: str, worker_id: str) -> bool:
        """Attempt a pessimistic lock on a pending job.

        Returns ``True`` if the lock was acquired (the row was in 'pending'
        state and was updated), or ``False`` if another worker beat us to it.
        """
        now = _now_iso()
        cursor = await self.db.execute(
            """
            UPDATE job_queue
            SET status = 'running', locked_by = ?, locked_at = ?, updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (worker_id, now, now, job_id),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def complete_job(self, job_id: str, result: dict) -> None:
        """Mark a job as completed and store its result payload."""
        await self.db.execute(
            """
            UPDATE job_queue
            SET status = 'completed', result = ?, updated_at = ?
            WHERE id = ?
            """,
            (json.dumps(result), _now_iso(), job_id),
        )
        await self.db.commit()

    async def fail_job(self, job_id: str, error: str) -> None:
        """Record a job failure and increment the attempt counter.

        If the job has not yet exhausted ``max_attempts``, it is reset to
        'pending' so the next poll will pick it up again.
        """
        cursor = await self.db.execute(
            "SELECT attempts, max_attempts FROM job_queue WHERE id = ?",
            (job_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return

        attempts = row[0] + 1
        max_attempts = row[1]
        new_status = "pending" if attempts < max_attempts else "failed"

        await self.db.execute(
            """
            UPDATE job_queue
            SET status = ?, attempts = ?, error_message = ?,
                locked_by = NULL, locked_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (new_status, attempts, error, _now_iso(), job_id),
        )
        await self.db.commit()

    async def mark_step(self, job_id: str, step: str, progress: int) -> None:
        """Update the current processing step and progress percentage."""
        await self.db.execute(
            "UPDATE job_queue SET current_step = ?, progress = ?, updated_at = ? WHERE id = ?",
            (step, progress, _now_iso(), job_id),
        )
        await self.db.commit()

    def _job_from_row(self, row: aiosqlite.Row) -> Job:
        """Build a Job model from a DB row, deserializing the result JSON."""
        data = self._row_to_dict(row)

        result: dict | None = None
        raw_result = data.get("result")
        if isinstance(raw_result, str):
            result = json.loads(raw_result)

        return Job(
            id=data["id"],
            track_id=data["track_id"],
            priority=data.get("priority", 1),
            status=data["status"],
            attempts=data.get("attempts", 0),
            max_attempts=data.get("max_attempts", 3),
            locked_by=data.get("locked_by"),
            locked_at=data.get("locked_at"),
            result=result,
            error_message=data.get("error_message"),
            current_step=data.get("current_step"),
            progress=data.get("progress", 0),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )
