"""Unit tests for SQLiteRepository — full CRUD coverage.

All tests use an in-memory aiosqlite database created in conftest.py.
asyncio_mode = "auto" (set in pytest.ini) means no explicit
@pytest.mark.asyncio decorator is needed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from karaoke_shared.models import (
    JobCreate,
    ParticipantCreate,
    PlayHistoryCreate,
    QueueEntryCreate,
    SessionCreate,
    TrackCreate,
    TrackUpdate,
)
from karaoke_shared.repositories import SQLiteRepository


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _track(
    artist: str = "Test Artist",
    title: str = "Test Title",
    source: str = "catalog",
    status: str = "pending",
    play_count: int = 0,
    lyrics_text: str | None = None,
) -> TrackCreate:
    return TrackCreate(
        artist=artist,
        title=title,
        source=source,
        status=status,
        play_count=play_count,
        lyrics_text=lyrics_text,
    )


def _session(room_id: str = "room-1") -> SessionCreate:
    return SessionCreate(room_id=room_id)


def _participant(session_id: str, display_name: str = "Alice") -> ParticipantCreate:
    return ParticipantCreate(session_id=session_id, display_name=display_name)


def _queue_entry(
    session_id: str,
    participant_id: str,
    track_id: str,
    status: str = "queued",
) -> QueueEntryCreate:
    return QueueEntryCreate(
        session_id=session_id,
        participant_id=participant_id,
        track_id=track_id,
        status=status,
    )


def _history(
    session_id: str, participant_id: str, track_id: str, completed: int = 0
) -> PlayHistoryCreate:
    return PlayHistoryCreate(
        session_id=session_id,
        participant_id=participant_id,
        track_id=track_id,
        completed=completed,
    )


def _job(track_id: str = "track-1", priority: int = 1) -> JobCreate:
    return JobCreate(track_id=track_id, priority=priority)


# ---------------------------------------------------------------------------
# Tracks
# ---------------------------------------------------------------------------


class TestTracks:
    async def test_create_and_get_track(self, sqlite_repo: SQLiteRepository):
        # Arrange
        data = _track(artist="Кино", title="Группа крови", source="catalog")

        # Act
        created = await sqlite_repo.create_track(data)
        fetched = await sqlite_repo.get_track(created.id)

        # Assert
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.artist == "Кино"
        assert fetched.title == "Группа крови"
        assert fetched.source == "catalog"
        assert fetched.status == "pending"
        assert fetched.play_count == 0
        assert fetched.qdrant_synced == 0

    async def test_get_track_not_found_returns_none(self, sqlite_repo: SQLiteRepository):
        result = await sqlite_repo.get_track(str(uuid.uuid4()))
        assert result is None

    async def test_update_track(self, sqlite_repo: SQLiteRepository):
        # Arrange
        created = await sqlite_repo.create_track(_track())
        original_updated_at = created.updated_at

        # Act
        updated = await sqlite_repo.update_track(
            created.id, TrackUpdate(status="ready", language="ru", play_count=5)
        )

        # Assert
        assert updated is not None
        assert updated.status == "ready"
        assert updated.language == "ru"
        assert updated.play_count == 5
        # Unchanged fields remain
        assert updated.artist == created.artist
        assert updated.title == created.title
        # updated_at must have been refreshed
        assert updated.updated_at >= original_updated_at

    async def test_update_track_not_found_returns_none(self, sqlite_repo: SQLiteRepository):
        result = await sqlite_repo.update_track(
            str(uuid.uuid4()), TrackUpdate(status="ready")
        )
        assert result is None

    async def test_list_popular_ordered_by_play_count(self, sqlite_repo: SQLiteRepository):
        # Arrange: three ready tracks with different play counts
        low = await sqlite_repo.create_track(
            _track(artist="Low", title="Low", status="ready", play_count=1)
        )
        high = await sqlite_repo.create_track(
            _track(artist="High", title="High", status="ready", play_count=100)
        )
        mid = await sqlite_repo.create_track(
            _track(artist="Mid", title="Mid", status="ready", play_count=50)
        )
        # Pending track should NOT appear
        await sqlite_repo.create_track(
            _track(artist="Pending", title="Pending", status="pending", play_count=999)
        )

        # Act
        results = await sqlite_repo.list_popular(limit=10)

        # Assert
        ids = [t.id for t in results]
        assert high.id in ids
        assert mid.id in ids
        assert low.id in ids
        # Pending track must not appear
        assert all(t.status == "ready" for t in results)
        # Order: descending by play_count
        counts = [t.play_count for t in results]
        assert counts == sorted(counts, reverse=True)

    async def test_list_popular_excludes_non_ready(self, sqlite_repo: SQLiteRepository):
        await sqlite_repo.create_track(
            _track(status="processing", play_count=999)
        )
        results = await sqlite_repo.list_popular()
        assert all(t.status == "ready" for t in results)

    async def test_search_fts(self, sqlite_repo: SQLiteRepository):
        # Arrange: ready track with distinct lyrics
        track = await sqlite_repo.create_track(
            _track(
                artist="Ария",
                title="Герой асфальта",
                status="ready",
                lyrics_text="герой асфальта едет быстро",
            )
        )

        # Act — search by a word in the lyrics
        results = await sqlite_repo.search_fts("герой")

        # Assert
        assert len(results) >= 1
        ids = [t.id for t in results]
        assert track.id in ids

    async def test_search_fts_matches_artist(self, sqlite_repo: SQLiteRepository):
        track = await sqlite_repo.create_track(
            _track(artist="Zemfira", title="Искала", status="ready")
        )
        results = await sqlite_repo.search_fts("Zemfira")
        ids = [t.id for t in results]
        assert track.id in ids

    async def test_search_fts_excludes_non_ready(self, sqlite_repo: SQLiteRepository):
        # pending track — should not be returned by FTS
        await sqlite_repo.create_track(
            _track(
                artist="Hidden",
                title="Hidden Song",
                status="pending",
                lyrics_text="hidden unique lyrics xyz",
            )
        )
        results = await sqlite_repo.search_fts("hidden unique lyrics xyz")
        assert all(t.status == "ready" for t in results)

    async def test_search_fts_invalid_query_returns_empty(
        self, sqlite_repo: SQLiteRepository
    ):
        # Malformed FTS5 query should return [] rather than crash
        results = await sqlite_repo.search_fts('"unclosed quote')
        assert results == []

    async def test_increment_play_count(self, sqlite_repo: SQLiteRepository):
        # Arrange
        track = await sqlite_repo.create_track(_track(play_count=0))
        assert track.play_count == 0

        # Act
        await sqlite_repo.increment_play_count(track.id)
        updated = await sqlite_repo.get_track(track.id)

        # Assert
        assert updated is not None
        assert updated.play_count == 1

    async def test_increment_play_count_multiple_times(self, sqlite_repo: SQLiteRepository):
        track = await sqlite_repo.create_track(_track(play_count=5))
        await sqlite_repo.increment_play_count(track.id)
        await sqlite_repo.increment_play_count(track.id)
        updated = await sqlite_repo.get_track(track.id)
        assert updated is not None
        assert updated.play_count == 7


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class TestSessions:
    async def test_create_and_get_session(self, sqlite_repo: SQLiteRepository):
        # Arrange
        data = _session(room_id="room-42")

        # Act
        created = await sqlite_repo.create_session(data)
        fetched = await sqlite_repo.get_session(created.id)

        # Assert
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.room_id == "room-42"
        assert fetched.status == "active"
        assert fetched.terminated_at is None

    async def test_get_session_not_found_returns_none(self, sqlite_repo: SQLiteRepository):
        result = await sqlite_repo.get_session(str(uuid.uuid4()))
        assert result is None

    async def test_terminate_session(self, sqlite_repo: SQLiteRepository):
        # Arrange
        session = await sqlite_repo.create_session(_session())
        assert session.status == "active"
        assert session.terminated_at is None

        # Act
        await sqlite_repo.terminate_session(session.id)
        updated = await sqlite_repo.get_session(session.id)

        # Assert
        assert updated is not None
        assert updated.status == "terminated"
        assert updated.terminated_at is not None

    async def test_get_active_by_room_returns_active(self, sqlite_repo: SQLiteRepository):
        # Arrange: create one active and one terminated session for the same room
        room = "room-99"
        active = await sqlite_repo.create_session(_session(room_id=room))
        terminated = await sqlite_repo.create_session(_session(room_id=room))
        await sqlite_repo.terminate_session(terminated.id)

        # Act
        result = await sqlite_repo.get_active_by_room(room)

        # Assert
        assert result is not None
        assert result.id == active.id
        assert result.status == "active"

    async def test_get_active_by_room_no_active_returns_none(
        self, sqlite_repo: SQLiteRepository
    ):
        room = "empty-room"
        session = await sqlite_repo.create_session(_session(room_id=room))
        await sqlite_repo.terminate_session(session.id)

        result = await sqlite_repo.get_active_by_room(room)
        assert result is None

    async def test_get_active_by_room_no_sessions_returns_none(
        self, sqlite_repo: SQLiteRepository
    ):
        result = await sqlite_repo.get_active_by_room("nonexistent-room")
        assert result is None


# ---------------------------------------------------------------------------
# Participants
# ---------------------------------------------------------------------------


class TestParticipants:
    async def test_create_and_get_participant(self, sqlite_repo: SQLiteRepository):
        # Arrange
        session = await sqlite_repo.create_session(_session())
        data = _participant(session_id=session.id, display_name="Bob")

        # Act
        created = await sqlite_repo.create_participant(data)
        fetched = await sqlite_repo.get_participant(created.id)

        # Assert
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.session_id == session.id
        assert fetched.display_name == "Bob"
        assert fetched.portrait_vector is None
        assert fetched.tracks_played == 0

    async def test_get_participant_not_found_returns_none(
        self, sqlite_repo: SQLiteRepository
    ):
        result = await sqlite_repo.get_participant(str(uuid.uuid4()))
        assert result is None

    async def test_get_participants_by_session(self, sqlite_repo: SQLiteRepository):
        # Arrange
        session = await sqlite_repo.create_session(_session())
        other_session = await sqlite_repo.create_session(_session(room_id="other"))

        p1 = await sqlite_repo.create_participant(
            _participant(session_id=session.id, display_name="Alice")
        )
        p2 = await sqlite_repo.create_participant(
            _participant(session_id=session.id, display_name="Bob")
        )
        # Participant in other session — should not appear
        await sqlite_repo.create_participant(
            _participant(session_id=other_session.id, display_name="Charlie")
        )

        # Act
        results = await sqlite_repo.get_participants_by_session(session.id)

        # Assert
        ids = {p.id for p in results}
        assert p1.id in ids
        assert p2.id in ids
        assert len(results) == 2

    async def test_increment_tracks_played(self, sqlite_repo: SQLiteRepository):
        # Arrange
        session = await sqlite_repo.create_session(_session())
        participant = await sqlite_repo.create_participant(
            _participant(session_id=session.id)
        )
        assert participant.tracks_played == 0

        # Act
        await sqlite_repo.increment_tracks_played(participant.id)
        updated = await sqlite_repo.get_participant(participant.id)

        # Assert
        assert updated is not None
        assert updated.tracks_played == 1

    async def test_increment_tracks_played_multiple(self, sqlite_repo: SQLiteRepository):
        session = await sqlite_repo.create_session(_session())
        participant = await sqlite_repo.create_participant(
            _participant(session_id=session.id)
        )
        await sqlite_repo.increment_tracks_played(participant.id)
        await sqlite_repo.increment_tracks_played(participant.id)
        await sqlite_repo.increment_tracks_played(participant.id)
        updated = await sqlite_repo.get_participant(participant.id)
        assert updated is not None
        assert updated.tracks_played == 3


# ---------------------------------------------------------------------------
# Queue entries
# ---------------------------------------------------------------------------


class TestQueueEntries:
    async def _setup(self, sqlite_repo: SQLiteRepository):
        """Create a session, participant, and track for queue tests."""
        session = await sqlite_repo.create_session(_session())
        participant = await sqlite_repo.create_participant(
            _participant(session_id=session.id)
        )
        track = await sqlite_repo.create_track(_track())
        return session, participant, track

    async def test_create_queue_entry_auto_position(self, sqlite_repo: SQLiteRepository):
        # Arrange
        session, participant, track = await self._setup(sqlite_repo)

        # Act: add two entries to the same session
        e1 = await sqlite_repo.create_queue_entry(
            _queue_entry(session.id, participant.id, track.id)
        )
        e2 = await sqlite_repo.create_queue_entry(
            _queue_entry(session.id, participant.id, track.id)
        )

        # Assert: positions are 1 and 2
        assert e1.order_position == 1
        assert e2.order_position == 2

    async def test_get_queue_by_session_only_active(self, sqlite_repo: SQLiteRepository):
        # Arrange
        session, participant, track = await self._setup(sqlite_repo)

        queued = await sqlite_repo.create_queue_entry(
            _queue_entry(session.id, participant.id, track.id, status="queued")
        )
        playing = await sqlite_repo.create_queue_entry(
            _queue_entry(session.id, participant.id, track.id, status="queued")
        )
        done = await sqlite_repo.create_queue_entry(
            _queue_entry(session.id, participant.id, track.id, status="queued")
        )

        # Mark one as playing, one as done
        await sqlite_repo.update_queue_entry_status(playing.id, "playing")
        await sqlite_repo.update_queue_entry_status(done.id, "done")

        # Act
        results = await sqlite_repo.get_queue_by_session(session.id)

        # Assert: only queued and playing entries returned
        ids = {e.id for e in results}
        assert queued.id in ids
        assert playing.id in ids
        assert done.id not in ids
        assert all(e.status in ("queued", "playing") for e in results)

    async def test_update_queue_entry_status_to_playing_sets_started_at(
        self, sqlite_repo: SQLiteRepository
    ):
        session, participant, track = await self._setup(sqlite_repo)
        entry = await sqlite_repo.create_queue_entry(
            _queue_entry(session.id, participant.id, track.id)
        )
        assert entry.started_at is None

        await sqlite_repo.update_queue_entry_status(entry.id, "playing")

        cursor = await sqlite_repo.db.execute(
            "SELECT started_at FROM queue_entries WHERE id = ?", (entry.id,)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] is not None  # started_at must be set

    async def test_update_queue_entry_status_to_done_sets_finished_at(
        self, sqlite_repo: SQLiteRepository
    ):
        session, participant, track = await self._setup(sqlite_repo)
        entry = await sqlite_repo.create_queue_entry(
            _queue_entry(session.id, participant.id, track.id)
        )
        await sqlite_repo.update_queue_entry_status(entry.id, "done")

        cursor = await sqlite_repo.db.execute(
            "SELECT finished_at, status FROM queue_entries WHERE id = ?", (entry.id,)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[1] == "done"
        assert row[0] is not None  # finished_at must be set

    async def test_update_queue_entry_status_to_skipped_sets_finished_at(
        self, sqlite_repo: SQLiteRepository
    ):
        session, participant, track = await self._setup(sqlite_repo)
        entry = await sqlite_repo.create_queue_entry(
            _queue_entry(session.id, participant.id, track.id)
        )
        await sqlite_repo.update_queue_entry_status(entry.id, "skipped")

        cursor = await sqlite_repo.db.execute(
            "SELECT finished_at FROM queue_entries WHERE id = ?", (entry.id,)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] is not None

    async def test_delete_queue_entry(self, sqlite_repo: SQLiteRepository):
        session, participant, track = await self._setup(sqlite_repo)
        entry = await sqlite_repo.create_queue_entry(
            _queue_entry(session.id, participant.id, track.id)
        )

        await sqlite_repo.delete_queue_entry(entry.id)

        # The internal getter should return None
        result = await sqlite_repo.get_queue_entry(entry.id)
        assert result is None

    async def test_get_current_entry_playing_has_priority(
        self, sqlite_repo: SQLiteRepository
    ):
        session, participant, track = await self._setup(sqlite_repo)

        # Add two queued entries, then mark the second as playing
        e1 = await sqlite_repo.create_queue_entry(
            _queue_entry(session.id, participant.id, track.id)
        )
        e2 = await sqlite_repo.create_queue_entry(
            _queue_entry(session.id, participant.id, track.id)
        )
        await sqlite_repo.update_queue_entry_status(e2.id, "playing")

        current = await sqlite_repo.get_current_entry(session.id)

        assert current is not None
        assert current.id == e2.id
        assert current.status == "playing"

    async def test_get_current_entry_returns_lowest_position_when_no_playing(
        self, sqlite_repo: SQLiteRepository
    ):
        session, participant, track = await self._setup(sqlite_repo)
        e1 = await sqlite_repo.create_queue_entry(
            _queue_entry(session.id, participant.id, track.id)
        )
        e2 = await sqlite_repo.create_queue_entry(
            _queue_entry(session.id, participant.id, track.id)
        )

        current = await sqlite_repo.get_current_entry(session.id)

        assert current is not None
        assert current.id == e1.id

    async def test_get_current_entry_empty_session_returns_none(
        self, sqlite_repo: SQLiteRepository
    ):
        session = await sqlite_repo.create_session(_session())
        result = await sqlite_repo.get_current_entry(session.id)
        assert result is None


# ---------------------------------------------------------------------------
# Play history
# ---------------------------------------------------------------------------


class TestPlayHistory:
    async def _setup(self, sqlite_repo: SQLiteRepository):
        session = await sqlite_repo.create_session(_session())
        participant = await sqlite_repo.create_participant(
            _participant(session_id=session.id)
        )
        track = await sqlite_repo.create_track(_track())
        return session, participant, track

    async def test_create_and_get_history(self, sqlite_repo: SQLiteRepository):
        session, participant, track = await self._setup(sqlite_repo)
        data = _history(session.id, participant.id, track.id, completed=1)

        created = await sqlite_repo.create_play_history(data)

        assert created.id == data.id
        assert created.session_id == session.id
        assert created.participant_id == participant.id
        assert created.track_id == track.id
        assert created.completed == 1

    async def test_get_history_by_participant_ordered(self, sqlite_repo: SQLiteRepository):
        """Entries returned in DESC order by played_at."""
        session, participant, track = await self._setup(sqlite_repo)

        # Insert 3 entries with explicit played_at timestamps (old → new)
        timestamps = [
            "2024-01-01T10:00:00+00:00",
            "2024-01-01T11:00:00+00:00",
            "2024-01-01T12:00:00+00:00",
        ]
        for ts in timestamps:
            phc = PlayHistoryCreate(
                session_id=session.id,
                participant_id=participant.id,
                track_id=track.id,
                played_at=ts,
            )
            await sqlite_repo.create_play_history(phc)

        results = await sqlite_repo.get_history_by_participant(participant.id)

        # Most recent first
        assert len(results) == 3
        played_ats = [r.played_at for r in results]
        assert played_ats == sorted(played_ats, reverse=True)

    async def test_get_history_by_participant_limit(self, sqlite_repo: SQLiteRepository):
        session, participant, track = await self._setup(sqlite_repo)

        for _ in range(5):
            await sqlite_repo.create_play_history(
                _history(session.id, participant.id, track.id)
            )

        results = await sqlite_repo.get_history_by_participant(participant.id, limit=3)
        assert len(results) == 3

    async def test_get_history_by_session(self, sqlite_repo: SQLiteRepository):
        session, participant, track = await self._setup(sqlite_repo)
        other_session = await sqlite_repo.create_session(_session(room_id="other"))
        other_participant = await sqlite_repo.create_participant(
            _participant(session_id=other_session.id)
        )

        await sqlite_repo.create_play_history(_history(session.id, participant.id, track.id))
        await sqlite_repo.create_play_history(_history(session.id, participant.id, track.id))
        # Entry in other session
        await sqlite_repo.create_play_history(
            _history(other_session.id, other_participant.id, track.id)
        )

        results = await sqlite_repo.get_history_by_session(session.id)
        assert len(results) == 2
        assert all(r.session_id == session.id for r in results)


# ---------------------------------------------------------------------------
# Job queue
#
# ---------------------------------------------------------------------------


class TestJobQueue:
    async def test_create_and_get_job(self, sqlite_repo: SQLiteRepository):
        data = _job(track_id="track-abc", priority=2)
        created = await sqlite_repo.create_job(data)

        fetched = await sqlite_repo.get_job(created.id)

        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.track_id == "track-abc"
        assert fetched.priority == 2
        assert fetched.status == "pending"
        assert fetched.attempts == 0
        assert fetched.max_attempts == 3
        assert fetched.locked_by is None
        assert fetched.locked_at is None
        assert fetched.result is None

    async def test_get_job_not_found_returns_none(self, sqlite_repo: SQLiteRepository):
        # get_job returns None early when no row found (before _job_from_row is called)
        result = await sqlite_repo.get_job(str(uuid.uuid4()))
        assert result is None

    async def test_poll_pending_high_priority_first(self, sqlite_repo: SQLiteRepository):
        # Arrange: low-priority job created first, high-priority second
        low = await sqlite_repo.create_job(_job(track_id="low", priority=1))
        high = await sqlite_repo.create_job(_job(track_id="high", priority=10))

        # Act
        results = await sqlite_repo.poll_pending(limit=2)

        # Assert: highest priority first
        assert len(results) == 2
        assert results[0].id == high.id
        assert results[1].id == low.id

    async def test_poll_pending_excludes_non_pending(self, sqlite_repo: SQLiteRepository):
        pending = await sqlite_repo.create_job(_job(track_id="p"))
        running = await sqlite_repo.create_job(_job(track_id="r"))
        await sqlite_repo.lock_job(running.id, "worker-1")

        results = await sqlite_repo.poll_pending()

        ids = [j.id for j in results]
        assert pending.id in ids
        assert running.id not in ids

    async def test_lock_job(self, sqlite_repo: SQLiteRepository):
        # Arrange
        job = await sqlite_repo.create_job(_job())

        # Act
        acquired = await sqlite_repo.lock_job(job.id, "worker-42")

        # Assert
        assert acquired is True
        updated = await sqlite_repo.get_job(job.id)
        assert updated is not None
        assert updated.status == "running"
        assert updated.locked_by == "worker-42"
        assert updated.locked_at is not None

    async def test_lock_job_already_locked_returns_false(
        self, sqlite_repo: SQLiteRepository
    ):
        job = await sqlite_repo.create_job(_job())
        first = await sqlite_repo.lock_job(job.id, "worker-1")
        assert first is True

        # Second lock attempt on same job
        second = await sqlite_repo.lock_job(job.id, "worker-2")
        assert second is False

        # The job is still locked by the first worker
        updated = await sqlite_repo.get_job(job.id)
        assert updated is not None
        assert updated.locked_by == "worker-1"

    async def test_complete_job(self, sqlite_repo: SQLiteRepository):
        job = await sqlite_repo.create_job(_job())
        await sqlite_repo.lock_job(job.id, "worker-1")

        result_payload = {"output_path": "/data/track.mp3", "duration": 240}
        await sqlite_repo.complete_job(job.id, result_payload)

        updated = await sqlite_repo.get_job(job.id)
        assert updated is not None
        assert updated.status == "completed"
        assert updated.result == result_payload

    async def test_fail_job_with_retry(self, sqlite_repo: SQLiteRepository):
        """First failure increments attempts and resets status to 'pending'."""
        job = await sqlite_repo.create_job(_job())
        await sqlite_repo.lock_job(job.id, "worker-1")
        assert job.max_attempts == 3

        # Act: first failure (attempts 0 -> 1, still < max_attempts=3)
        await sqlite_repo.fail_job(job.id, "connection timeout")

        updated = await sqlite_repo.get_job(job.id)
        assert updated is not None
        assert updated.status == "pending"
        assert updated.attempts == 1
        assert updated.error_message == "connection timeout"
        # Lock cleared on retry
        assert updated.locked_by is None
        assert updated.locked_at is None

    async def test_fail_job_exhausted(self, sqlite_repo: SQLiteRepository):
        """Job transitions to 'failed' when attempts reach max_attempts."""
        # Start a job with max_attempts=2
        data = JobCreate(track_id="t1", max_attempts=2)
        job = await sqlite_repo.create_job(data)

        # First failure: attempts=1, still < 2 → pending
        await sqlite_repo.lock_job(job.id, "w1")
        await sqlite_repo.fail_job(job.id, "error 1")

        after_first = await sqlite_repo.get_job(job.id)
        assert after_first is not None
        assert after_first.status == "pending"
        assert after_first.attempts == 1

        # Second failure: attempts=2, == max_attempts → failed
        await sqlite_repo.lock_job(job.id, "w2")
        await sqlite_repo.fail_job(job.id, "error 2")

        after_second = await sqlite_repo.get_job(job.id)
        assert after_second is not None
        assert after_second.status == "failed"
        assert after_second.attempts == 2
        assert after_second.error_message == "error 2"

