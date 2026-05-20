"""Async PostgreSQL repository.

All database I/O goes through an injected ``asyncpg.Pool``.
The pool is created once at application startup (lifespan) and shared
across requests via FastAPI's ``app.state.pg_pool``.

Usage::

    repo = PgRepository(pool)
    track = await repo.create_track(
        TrackCreate(artist="...", title="...", source="catalog")
    )
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import asyncpg

from karaoke_shared.models.job import Job, JobCreate
from karaoke_shared.models.play_history import PlayHistoryCreate, PlayHistoryEntry
from karaoke_shared.models.queue import QueueEntry, QueueEntryCreate
from karaoke_shared.models.session import (
    Participant,
    ParticipantCreate,
    Session,
    SessionCreate,
)
from karaoke_shared.constants import (
    JobStatus,
    QueueEntryStatus,
    SessionStatus,
    TrackStatus,
)
from karaoke_shared.models.alignment import AlignmentDocument, AlignmentRevision
from karaoke_shared.models.catalog_cluster import CatalogCluster
from karaoke_shared.models.track import SyllableTiming, Track, TrackCreate, TrackUpdate


def _now_dt() -> datetime:
    """Return the current UTC time as a datetime object."""
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return _now_dt().isoformat()


def _to_dt(value: Any) -> datetime | None:
    """Convert an ISO string or datetime to a datetime object for asyncpg."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return None


def _ts(value: Any) -> str | None:
    """Convert a datetime or string timestamp to ISO string for model output."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class PgRepository:
    """Async repository backed by an ``asyncpg.Pool``.

    The pool is injected rather than created here so that all requests
    within the same process share one connection pool.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    # ------------------------------------------------------------------
    # Tracks
    # ------------------------------------------------------------------

    async def create_track(self, data: TrackCreate) -> Track:
        """Insert a new track row and return the full Track model."""
        syllable_timings_json = None
        if data.syllable_timings is not None:
            syllable_timings_json = json.dumps(
                [st.model_dump() for st in data.syllable_timings]
            )

        await self.pool.execute(
            """
            INSERT INTO tracks (
                id, artist, title, duration_sec, instrumental_key, review_vocal_key,
                lyrics_text, lyrics_source, syllable_timings, language, source,
                status, error_message, play_count, qdrant_synced,
                popularity_category, chart_count, chart_last_seen,
                catalog_cluster_id, rec_cluster_id, created_at, updated_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6,
                $7, $8, $9, $10, $11,
                $12, $13, $14, $15,
                $16, $17, $18,
                $19, $20, $21, $22
            )
            """,
            data.id, data.artist, data.title, data.duration_sec,
            data.instrumental_key, data.review_vocal_key,
            data.lyrics_text, data.lyrics_source, syllable_timings_json,
            data.language, data.source,
            data.status, None, data.play_count, data.qdrant_synced,
            data.popularity_category, data.chart_count, _to_dt(data.chart_last_seen),
            data.catalog_cluster_id, data.rec_cluster_id,
            _to_dt(data.created_at), _to_dt(data.updated_at),
        )

        track = await self.get_track(data.id)
        if track is None:
            raise RuntimeError(f"Track {data.id} not found after insert")
        return track

    async def get_track(self, track_id: str) -> Track | None:
        """Return a single track by primary key, or ``None`` if not found."""
        row = await self.pool.fetchrow(
            "SELECT * FROM tracks WHERE id = $1", track_id
        )
        if row is None:
            return None
        return self._track_from_row(row)

    async def update_track(self, track_id: str, data: TrackUpdate) -> Track | None:
        """Apply a partial update to a track and return the updated model."""
        updates: dict[str, Any] = {"updated_at": _to_dt(data.updated_at)}

        for field in (
            "artist", "title", "duration_sec", "instrumental_key",
            "review_vocal_key",
            "lyrics_text", "lyrics_source", "language", "source",
            "status", "error_message",
            "play_count", "qdrant_synced", "popularity_category",
            "chart_count", "chart_last_seen", "catalog_cluster_id",
            "rec_cluster_id",
        ):
            value = getattr(data, field)
            if value is not None:
                # Convert timestamp strings to datetime for asyncpg
                if field == "chart_last_seen":
                    value = _to_dt(value)
                updates[field] = value

        if data.syllable_timings is not None:
            updates["syllable_timings"] = json.dumps(
                [st.model_dump() for st in data.syllable_timings]
            )

        if len(updates) <= 1:
            return await self.get_track(track_id)

        set_parts = []
        values = []
        for i, (col, val) in enumerate(updates.items(), start=1):
            set_parts.append(f"{col} = ${i}")
            values.append(val)
        values.append(track_id)
        set_clause = ", ".join(set_parts)

        await self.pool.execute(
            f"UPDATE tracks SET {set_clause} WHERE id = ${len(values)}",  # noqa: S608
            *values,
        )

        return await self.get_track(track_id)

    async def list_popular(
        self, limit: int = 10, categories: list[str] | None = None,
    ) -> list[Track]:
        """Return the most-played ready tracks, ordered by play count descending."""
        if categories:
            rows = await self.pool.fetch(
                "SELECT * FROM tracks WHERE status = $1 AND popularity_category = ANY($2)"
                " ORDER BY play_count DESC, RANDOM() LIMIT $3",
                TrackStatus.READY, categories, limit,
            )
        else:
            rows = await self.pool.fetch(
                "SELECT * FROM tracks WHERE status = $1 ORDER BY play_count DESC, RANDOM() LIMIT $2",
                TrackStatus.READY, limit,
            )
        return [self._track_from_row(row) for row in rows]

    async def list_random(
        self, limit: int = 10, categories: list[str] | None = None,
    ) -> list[Track]:
        """Return random ready tracks from the catalog."""
        if categories:
            rows = await self.pool.fetch(
                "SELECT * FROM tracks WHERE status = $1 AND popularity_category = ANY($2)"
                " ORDER BY RANDOM() LIMIT $3",
                TrackStatus.READY, categories, limit,
            )
        else:
            rows = await self.pool.fetch(
                "SELECT * FROM tracks WHERE status = $1 ORDER BY RANDOM() LIMIT $2",
                TrackStatus.READY, limit,
            )
        return [self._track_from_row(row) for row in rows]

    async def get_tracks_by_cluster(
        self,
        cluster_id: int,
        limit: int = 20,
        exclude_ids: set[str] | None = None,
        exclude_artists: set[str] | None = None,
        language: str | None = None,
    ) -> list[Track]:
        """Return tracks from a rec cluster, well-known first, then random."""
        conditions = ["status = $1", "rec_cluster_id = $2"]
        params: list[Any] = [TrackStatus.READY, cluster_id]
        idx = 3

        if exclude_ids:
            conditions.append(f"id != ALL(${idx})")
            params.append(list(exclude_ids))
            idx += 1

        if exclude_artists:
            conditions.append(f"artist != ALL(${idx})")
            params.append(list(exclude_artists))
            idx += 1

        if language:
            conditions.append(f"language = ${idx}")
            params.append(language)
            idx += 1

        where = " AND ".join(conditions)
        params.append(limit)

        rows = await self.pool.fetch(
            f"""
            SELECT * FROM tracks
            WHERE {where}
            ORDER BY
                CASE popularity_category
                    WHEN 'eternal_hit' THEN 0
                    WHEN 'current_hit' THEN 1
                    WHEN 'artist_best' THEN 2
                    WHEN 'former_hit' THEN 3
                    ELSE 4
                END,
                RANDOM()
            LIMIT ${idx}
            """,
            *params,
        )
        return [self._track_from_row(row) for row in rows]

    async def get_tracks_by_ids(self, track_ids: list[str]) -> dict[str, Track]:
        """Return a dict of ``{track_id: Track}`` for the given IDs."""
        if not track_ids:
            return {}
        rows = await self.pool.fetch(
            "SELECT * FROM tracks WHERE id = ANY($1)",
            track_ids,
        )
        return {t.id: t for t in (self._track_from_row(r) for r in rows)}

    # Custom ts_rank weights: {D, C, B, A} where tsvector weights are
    # artist='A', title='B', lyrics='C'.  Priority: artist > title > lyrics.
    _TS_RANK_WEIGHTS = "'{0, 0.1, 0.4, 1.0}'"

    async def search_fts(
        self, query: str, limit: int = 20, offset: int = 0,
    ) -> list[Track]:
        """Full-text search over artist, title, and lyrics using tsvector."""
        import re
        tokens = re.findall(r'[\w.]+', query, re.UNICODE)
        if not tokens:
            return []

        try:
            rows = await self.pool.fetch(
                f"""
                SELECT *, ts_rank({self._TS_RANK_WEIGHTS}, search_vector,
                                  plainto_tsquery('simple', $1)) AS rank
                FROM tracks
                WHERE search_vector @@ plainto_tsquery('simple', $1)
                  AND status = $2
                ORDER BY rank DESC
                LIMIT $3 OFFSET $4
                """,
                query, TrackStatus.READY, limit, offset,
            )
        except Exception:
            return []
        return [self._track_from_row(row) for row in rows]

    async def search_fts_count(self, query: str) -> int:
        """Count total FTS matches (for pagination)."""
        import re
        tokens = re.findall(r'[\w.]+', query, re.UNICODE)
        if not tokens:
            return 0

        try:
            count = await self.pool.fetchval(
                """
                SELECT COUNT(*) FROM tracks
                WHERE search_vector @@ plainto_tsquery('simple', $1)
                  AND status = $2
                """,
                query, TrackStatus.READY,
            )
            return count or 0
        except Exception:
            return 0

    async def update_popularity(
        self,
        track_id: str,
        category: str,
        chart_count: int = 0,
        chart_last_seen: str | None = None,
    ) -> None:
        """Update popularity fields for a track."""
        await self.pool.execute(
            """
            UPDATE tracks
            SET popularity_category = $1, chart_count = $2, chart_last_seen = $3, updated_at = $4
            WHERE id = $5
            """,
            category, chart_count, _to_dt(chart_last_seen), _now_dt(), track_id,
        )

    async def increment_play_count(self, track_id: str) -> None:
        """Increment the play_count counter for a track by 1."""
        await self.pool.execute(
            "UPDATE tracks SET play_count = play_count + 1, updated_at = $1 WHERE id = $2",
            _now_dt(), track_id,
        )

    async def suggest_tracks(
        self, query: str, limit: int = 10
    ) -> list[dict[str, str]]:
        """Prefix search on artist and title for autocomplete."""
        pattern = f"%{query}%"
        rows = await self.pool.fetch(
            """
            SELECT DISTINCT artist, title FROM tracks
            WHERE (artist ILIKE $1 OR title ILIKE $1)
              AND status = $2
            LIMIT $3
            """,
            pattern, TrackStatus.READY, limit,
        )
        return [{"artist": row["artist"], "title": row["title"]} for row in rows]

    def _track_from_row(self, row: asyncpg.Record) -> Track:
        """Build a Track model from a DB row, deserializing JSON fields."""
        syllable_timings: list[SyllableTiming] | None = None
        raw_timings = row.get("syllable_timings")
        if raw_timings is not None:
            if isinstance(raw_timings, str):
                parsed = json.loads(raw_timings)
            else:
                parsed = raw_timings
            syllable_timings = [SyllableTiming(**item) for item in parsed]

        return Track(
            id=row["id"],
            artist=row["artist"],
            title=row["title"],
            duration_sec=row.get("duration_sec"),
            instrumental_key=row.get("instrumental_key"),
            review_vocal_key=row.get("review_vocal_key"),
            lyrics_text=row.get("lyrics_text"),
            lyrics_source=row.get("lyrics_source"),
            syllable_timings=syllable_timings,
            language=row.get("language"),
            source=row["source"],
            status=row["status"],
            error_message=row.get("error_message"),
            play_count=row.get("play_count", 0),
            qdrant_synced=row.get("qdrant_synced", 0),
            popularity_category=row.get("popularity_category", "regular"),
            chart_count=row.get("chart_count", 0),
            chart_last_seen=_ts(row.get("chart_last_seen")),
            catalog_cluster_id=row.get("catalog_cluster_id"),
            rec_cluster_id=row.get("rec_cluster_id"),
            created_at=_ts(row["created_at"]),
            updated_at=_ts(row["updated_at"]),
        )

    # ------------------------------------------------------------------
    # Alignment revisions
    # ------------------------------------------------------------------

    async def create_alignment_revision(
        self,
        revision: AlignmentRevision,
    ) -> AlignmentRevision:
        """Insert an alignment revision and return the stored model."""
        await self.pool.execute(
            """
            INSERT INTO alignment_revisions (
                id, track_id, revision_no, source, lyrics_text, syllable_timings,
                document, operations, diagnostics, is_published, created_by,
                created_at, updated_at, published_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6,
                $7, $8, $9, $10, $11,
                $12, $13, $14
            )
            """,
            revision.id,
            revision.track_id,
            revision.revision_no,
            revision.source,
            revision.lyrics_text,
            json.dumps([st.model_dump() for st in revision.syllable_timings]),
            json.dumps(revision.document.model_dump()) if revision.document else None,
            json.dumps(revision.operations),
            json.dumps(revision.diagnostics),
            revision.is_published,
            revision.created_by,
            _to_dt(revision.created_at),
            _to_dt(revision.updated_at),
            _to_dt(revision.published_at),
        )
        stored = await self.get_alignment_revision(revision.id)
        if stored is None:
            raise RuntimeError(f"AlignmentRevision {revision.id} not found after insert")
        return stored

    async def next_alignment_revision_no(self, track_id: str) -> int:
        """Return the next revision number for a track."""
        value = await self.pool.fetchval(
            (
                "SELECT COALESCE(MAX(revision_no), 0) + 1 "
                "FROM alignment_revisions WHERE track_id = $1"
            ),
            track_id,
        )
        return int(value or 1)

    async def get_alignment_revision(
        self, revision_id: str
    ) -> AlignmentRevision | None:
        """Return one alignment revision by id."""
        row = await self.pool.fetchrow(
            "SELECT * FROM alignment_revisions WHERE id = $1", revision_id
        )
        if row is None:
            return None
        return self._alignment_revision_from_row(row)

    async def list_alignment_revisions(self, track_id: str) -> list[AlignmentRevision]:
        """Return revisions for a track, newest first."""
        rows = await self.pool.fetch(
            """
            SELECT * FROM alignment_revisions
            WHERE track_id = $1
            ORDER BY revision_no DESC
            """,
            track_id,
        )
        return [self._alignment_revision_from_row(row) for row in rows]

    async def get_published_alignment_revision(
        self, track_id: str
    ) -> AlignmentRevision | None:
        """Return the currently published alignment revision for a track."""
        row = await self.pool.fetchrow(
            """
            SELECT * FROM alignment_revisions
            WHERE track_id = $1 AND is_published = TRUE
            ORDER BY revision_no DESC
            LIMIT 1
            """,
            track_id,
        )
        if row is None:
            return None
        return self._alignment_revision_from_row(row)

    async def publish_alignment_revision(
        self, track_id: str, revision_id: str
    ) -> AlignmentRevision | None:
        """Publish a revision and copy it into the track snapshot."""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT * FROM alignment_revisions
                WHERE id = $1 AND track_id = $2
                """,
                revision_id,
                track_id,
            )
            if row is None:
                return None
            now = _now_dt()
            await conn.execute(
                "UPDATE alignment_revisions SET is_published = FALSE WHERE track_id = $1",
                track_id,
            )
            await conn.execute(
                """
                UPDATE alignment_revisions
                SET is_published = TRUE, published_at = $1, updated_at = $1
                WHERE id = $2
                """,
                now,
                revision_id,
            )
            await conn.execute(
                """
                UPDATE tracks
                SET lyrics_text = $1, syllable_timings = $2, updated_at = $3
                WHERE id = $4
                """,
                row.get("lyrics_text"),
                row.get("syllable_timings"),
                now,
                track_id,
            )
        return await self.get_alignment_revision(revision_id)

    def _alignment_revision_from_row(self, row: asyncpg.Record) -> AlignmentRevision:
        raw_timings = row.get("syllable_timings") or []
        if isinstance(raw_timings, str):
            raw_timings = json.loads(raw_timings)
        raw_document = row.get("document")
        if isinstance(raw_document, str):
            raw_document = json.loads(raw_document)
        raw_operations = row.get("operations") or []
        if isinstance(raw_operations, str):
            raw_operations = json.loads(raw_operations)
        raw_diagnostics = row.get("diagnostics") or {}
        if isinstance(raw_diagnostics, str):
            raw_diagnostics = json.loads(raw_diagnostics)

        return AlignmentRevision(
            id=row["id"],
            track_id=row["track_id"],
            revision_no=row["revision_no"],
            source=row["source"],
            lyrics_text=row.get("lyrics_text"),
            syllable_timings=[SyllableTiming(**item) for item in raw_timings],
            document=AlignmentDocument(**raw_document) if raw_document else None,
            operations=raw_operations,
            diagnostics=raw_diagnostics,
            is_published=row.get("is_published", False),
            created_by=row.get("created_by"),
            created_at=_ts(row["created_at"]),
            updated_at=_ts(row["updated_at"]),
            published_at=_ts(row.get("published_at")),
        )

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    async def create_session(self, data: SessionCreate) -> Session:
        """Insert a new session row and return the Session model."""
        await self.pool.execute(
            """
            INSERT INTO sessions (id, room_id, status, created_at, terminated_at)
            VALUES ($1, $2, $3, $4, $5)
            """,
            data.id, data.room_id, data.status, _to_dt(data.created_at), None,
        )
        session = await self.get_session(data.id)
        if session is None:
            raise RuntimeError(f"Session {data.id} not found after insert")
        return session

    async def get_session(self, session_id: str) -> Session | None:
        """Return a single session by primary key, or ``None`` if not found."""
        row = await self.pool.fetchrow(
            "SELECT * FROM sessions WHERE id = $1", session_id
        )
        if row is None:
            return None
        return self._session_from_row(row)

    async def terminate_session(self, session_id: str) -> None:
        """Mark a session as terminated and record the termination timestamp."""
        await self.pool.execute(
            "UPDATE sessions SET status = $1, terminated_at = $2 WHERE id = $3",
            SessionStatus.TERMINATED, _now_dt(), session_id,
        )

    async def get_active_by_room(self, room_id: str) -> Session | None:
        """Return the active session for a room, or ``None`` if none exists."""
        row = await self.pool.fetchrow(
            "SELECT * FROM sessions WHERE room_id = $1 AND status = $2 LIMIT 1",
            room_id, SessionStatus.ACTIVE,
        )
        if row is None:
            return None
        return self._session_from_row(row)

    def _session_from_row(self, row: asyncpg.Record) -> Session:
        return Session(
            id=row["id"],
            room_id=row["room_id"],
            status=row["status"],
            created_at=_ts(row["created_at"]),
            terminated_at=_ts(row.get("terminated_at")),
        )

    # ------------------------------------------------------------------
    # Participants
    # ------------------------------------------------------------------

    async def create_participant(self, data: ParticipantCreate) -> Participant:
        """Insert a new participant row and return the Participant model."""
        await self.pool.execute(
            """
            INSERT INTO participants (
                id, session_id, display_name, portrait_vector,
                lyrics_portrait_vector, tracks_played, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            data.id, data.session_id, data.display_name,
            None, None, data.tracks_played, _to_dt(data.created_at),
        )
        participant = await self.get_participant(data.id)
        if participant is None:
            raise RuntimeError(f"Participant {data.id} not found after insert")
        return participant

    async def get_participants_by_session(self, session_id: str) -> list[Participant]:
        """Return all participants belonging to a session."""
        rows = await self.pool.fetch(
            "SELECT * FROM participants WHERE session_id = $1",
            session_id,
        )
        return [self._participant_from_row(row) for row in rows]

    async def get_participant(self, participant_id: str) -> Participant | None:
        """Return a single participant by primary key, or ``None`` if not found."""
        row = await self.pool.fetchrow(
            "SELECT * FROM participants WHERE id = $1", participant_id
        )
        if row is None:
            return None
        return self._participant_from_row(row)

    async def get_participants_by_ids(
        self, participant_ids: list[str]
    ) -> dict[str, Participant]:
        """Return a dict of ``{participant_id: Participant}`` for the given IDs."""
        if not participant_ids:
            return {}
        rows = await self.pool.fetch(
            "SELECT * FROM participants WHERE id = ANY($1)",
            participant_ids,
        )
        return {p.id: p for p in (self._participant_from_row(r) for r in rows)}

    async def increment_tracks_played(self, participant_id: str) -> None:
        """Increment the tracks_played counter for a participant by 1."""
        await self.pool.execute(
            "UPDATE participants SET tracks_played = tracks_played + 1 WHERE id = $1",
            participant_id,
        )

    async def update_participant_portrait(
        self,
        participant_id: str,
        portrait_vector: list[float] | None = None,
        lyrics_portrait_vector: list[float] | None = None,
    ) -> None:
        """Update the portrait vector(s) for a participant."""
        if portrait_vector is not None:
            await self.pool.execute(
                "UPDATE participants SET portrait_vector = $1 WHERE id = $2",
                json.dumps(portrait_vector), participant_id,
            )
        if lyrics_portrait_vector is not None:
            await self.pool.execute(
                "UPDATE participants SET lyrics_portrait_vector = $1 WHERE id = $2",
                json.dumps(lyrics_portrait_vector), participant_id,
            )

    def _participant_from_row(self, row: asyncpg.Record) -> Participant:
        portrait_vector: list[float] | None = None
        raw_portrait = row.get("portrait_vector")
        if raw_portrait is not None:
            if isinstance(raw_portrait, str):
                portrait_vector = json.loads(raw_portrait)
            else:
                portrait_vector = raw_portrait

        lyrics_portrait_vector: list[float] | None = None
        raw_lyrics = row.get("lyrics_portrait_vector")
        if raw_lyrics is not None:
            if isinstance(raw_lyrics, str):
                lyrics_portrait_vector = json.loads(raw_lyrics)
            else:
                lyrics_portrait_vector = raw_lyrics

        return Participant(
            id=row["id"],
            session_id=row["session_id"],
            display_name=row["display_name"],
            portrait_vector=portrait_vector,
            lyrics_portrait_vector=lyrics_portrait_vector,
            tracks_played=row.get("tracks_played", 0),
            created_at=_ts(row["created_at"]),
        )

    # ------------------------------------------------------------------
    # Queue entries
    # ------------------------------------------------------------------

    async def create_queue_entry(self, data: QueueEntryCreate) -> QueueEntry:
        """Add an entry to the queue and return the full QueueEntry model."""
        await self.pool.execute(
            """
            INSERT INTO queue_entries (
                id, session_id, participant_id, track_id,
                order_position, status, added_at, started_at, finished_at
            ) VALUES (
                $1, $2, $3, $4,
                (SELECT COALESCE(MAX(order_position), 0) + 1
                 FROM queue_entries WHERE session_id = $2),
                $5, $6, $7, $8
            )
            """,
            data.id, data.session_id, data.participant_id, data.track_id,
            data.status, _to_dt(data.added_at), None, None,
        )
        entry = await self.get_queue_entry(data.id)
        if entry is None:
            raise RuntimeError(f"QueueEntry {data.id} not found after insert")
        return entry

    async def get_queue_by_session(self, session_id: str) -> list[QueueEntry]:
        """Return queued and playing entries for a session, ordered by position."""
        rows = await self.pool.fetch(
            """
            SELECT * FROM queue_entries
            WHERE session_id = $1
              AND status IN ($2, $3)
            ORDER BY order_position
            """,
            session_id, QueueEntryStatus.QUEUED, QueueEntryStatus.PLAYING,
        )
        return [self._queue_entry_from_row(row) for row in rows]

    async def update_queue_entry_status(self, entry_id: str, status: str) -> None:
        """Update the status of a queue entry."""
        now = _now_dt()

        if status == QueueEntryStatus.PLAYING:
            await self.pool.execute(
                "UPDATE queue_entries SET status = $1, started_at = $2 WHERE id = $3",
                status, now, entry_id,
            )
        elif status in (QueueEntryStatus.DONE, QueueEntryStatus.SKIPPED):
            await self.pool.execute(
                "UPDATE queue_entries SET status = $1, finished_at = $2 WHERE id = $3",
                status, now, entry_id,
            )
        else:
            await self.pool.execute(
                "UPDATE queue_entries SET status = $1 WHERE id = $2",
                status, entry_id,
            )

    async def delete_queue_entry(self, entry_id: str) -> None:
        """Permanently remove a queue entry."""
        await self.pool.execute(
            "DELETE FROM queue_entries WHERE id = $1", entry_id
        )

    async def get_current_entry(self, session_id: str) -> QueueEntry | None:
        """Return the currently playing entry, or the next queued entry."""
        row = await self.pool.fetchrow(
            """
            SELECT * FROM queue_entries
            WHERE session_id = $1
              AND status IN ($2, $3)
            ORDER BY
                CASE status WHEN $2 THEN 0 ELSE 1 END,
                order_position
            LIMIT 1
            """,
            session_id, QueueEntryStatus.PLAYING, QueueEntryStatus.QUEUED,
        )
        if row is None:
            return None
        return self._queue_entry_from_row(row)

    async def get_queue_entry(self, entry_id: str) -> QueueEntry | None:
        """Fetch a single queue entry by primary key."""
        row = await self.pool.fetchrow(
            "SELECT * FROM queue_entries WHERE id = $1", entry_id
        )
        if row is None:
            return None
        return self._queue_entry_from_row(row)

    def _queue_entry_from_row(self, row: asyncpg.Record) -> QueueEntry:
        return QueueEntry(
            id=row["id"],
            session_id=row["session_id"],
            participant_id=row["participant_id"],
            track_id=row["track_id"],
            order_position=row["order_position"],
            status=row["status"],
            added_at=_ts(row["added_at"]),
            started_at=_ts(row.get("started_at")),
            finished_at=_ts(row.get("finished_at")),
        )

    # ------------------------------------------------------------------
    # Play history
    # ------------------------------------------------------------------

    async def create_play_history(self, data: PlayHistoryCreate) -> PlayHistoryEntry:
        """Insert a play history record and return the full model."""
        await self.pool.execute(
            """
            INSERT INTO play_history (
                id, session_id, participant_id, track_id, played_at, completed
            ) VALUES ($1, $2, $3, $4, $5, $6)
            """,
            data.id, data.session_id, data.participant_id,
            data.track_id, _to_dt(data.played_at), data.completed,
        )
        row = await self.pool.fetchrow(
            "SELECT * FROM play_history WHERE id = $1", data.id
        )
        if row is None:
            raise RuntimeError(f"PlayHistoryEntry {data.id} not found after insert")
        return self._play_history_from_row(row)

    async def get_history_by_participant(
        self, participant_id: str, limit: int = 20
    ) -> list[PlayHistoryEntry]:
        """Return the most recent play history entries for a participant."""
        rows = await self.pool.fetch(
            """
            SELECT * FROM play_history
            WHERE participant_id = $1
            ORDER BY played_at DESC
            LIMIT $2
            """,
            participant_id, limit,
        )
        return [self._play_history_from_row(row) for row in rows]

    async def get_history_by_session(self, session_id: str) -> list[PlayHistoryEntry]:
        """Return all play history entries for a session."""
        rows = await self.pool.fetch(
            "SELECT * FROM play_history WHERE session_id = $1",
            session_id,
        )
        return [self._play_history_from_row(row) for row in rows]

    def _play_history_from_row(self, row: asyncpg.Record) -> PlayHistoryEntry:
        return PlayHistoryEntry(
            id=row["id"],
            session_id=row["session_id"],
            participant_id=row["participant_id"],
            track_id=row["track_id"],
            played_at=_ts(row["played_at"]),
            completed=row.get("completed", 0),
        )

    # ------------------------------------------------------------------
    # Catalog clusters
    # ------------------------------------------------------------------

    async def create_catalog_cluster(
        self,
        centroid_audio: list[float],
        centroid_lyrics: list[float],
        track_count: int,
    ) -> int:
        """Insert a catalog cluster and return its auto-generated ID."""
        now = _now_dt()
        row = await self.pool.fetchrow(
            """
            INSERT INTO catalog_clusters (centroid_audio, centroid_lyrics, track_count, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            json.dumps(centroid_audio), json.dumps(centroid_lyrics), track_count, now, now,
        )
        return row["id"]

    async def get_all_clusters(self) -> list[CatalogCluster]:
        """Return all catalog clusters as CatalogCluster models."""
        rows = await self.pool.fetch("SELECT * FROM catalog_clusters ORDER BY id")
        results: list[CatalogCluster] = []
        for row in rows:
            ca = row["centroid_audio"]
            cl = row["centroid_lyrics"]
            results.append(CatalogCluster(
                id=row["id"],
                centroid_audio=json.loads(ca) if isinstance(ca, str) else ca,
                centroid_lyrics=json.loads(cl) if isinstance(cl, str) else cl,
                track_count=row["track_count"],
                created_at=_ts(row["created_at"]),
                updated_at=_ts(row["updated_at"]),
            ))
        return results

    async def clear_clusters(self) -> None:
        """Delete all catalog clusters, their mood tags, and reset track assignments."""
        await self.pool.execute("DELETE FROM mood_tags")
        await self.pool.execute("DELETE FROM catalog_clusters")
        await self.pool.execute("UPDATE tracks SET catalog_cluster_id = NULL")

    async def assign_cluster(self, track_id: str, cluster_id: int) -> None:
        """Assign a track to a catalog cluster."""
        await self.pool.execute(
            "UPDATE tracks SET catalog_cluster_id = $1 WHERE id = $2",
            cluster_id, track_id,
        )

    # ------------------------------------------------------------------
    # Artists
    # ------------------------------------------------------------------

    async def upsert_artist(
        self, name: str, image_path: str | None = None, source: str | None = None
    ) -> None:
        """Insert or update an artist record."""
        now = _now_dt()
        await self.pool.execute(
            """
            INSERT INTO artists (name, image_path, source, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT(name) DO UPDATE SET image_path = $2, source = $3, updated_at = $5
            """,
            name, image_path, source, now, now,
        )

    async def get_artist(self, name: str) -> dict | None:
        """Return artist record by name."""
        row = await self.pool.fetchrow("SELECT * FROM artists WHERE name = $1", name)
        if row is None:
            return None
        return dict(row)

    async def get_artists_by_names(self, names: list[str]) -> dict[str, dict]:
        """Return a dict of ``{name: artist_dict}`` for the given names."""
        if not names:
            return {}
        rows = await self.pool.fetch(
            "SELECT * FROM artists WHERE name = ANY($1)",
            list(names),
        )
        return {dict(r)["name"]: dict(r) for r in rows}

    async def get_artists_without_images(self, limit: int = 100) -> list[str]:
        """Return artist names that have no image_path set."""
        rows = await self.pool.fetch(
            """
            SELECT DISTINCT t.artist FROM tracks t
            LEFT JOIN artists a ON t.artist = a.name
            WHERE (a.image_path IS NULL OR a.name IS NULL)
              AND t.status = 'ready'
            LIMIT $1
            """,
            limit,
        )
        return [row[0] for row in rows]

    # ------------------------------------------------------------------
    # Mood tags
    # ------------------------------------------------------------------

    async def create_mood_tag(self, name: str, cluster_id: int) -> int:
        """Insert a mood tag and return its auto-generated ID."""
        now = _now_dt()
        row = await self.pool.fetchrow(
            "INSERT INTO mood_tags (name, cluster_id, created_at) VALUES ($1, $2, $3) RETURNING id",
            name, cluster_id, now,
        )
        return row["id"]

    async def get_all_tags(self) -> list[dict]:
        """Return all mood tags."""
        rows = await self.pool.fetch("SELECT * FROM mood_tags ORDER BY id")
        return [dict(row) for row in rows]

    async def get_tags_excluding_clusters(
        self, excluded_cluster_ids: set[int], limit: int = 8
    ) -> list[dict]:
        """Return tags from clusters NOT in the excluded set."""
        if excluded_cluster_ids:
            rows = await self.pool.fetch(
                "SELECT * FROM mood_tags WHERE cluster_id != ALL($1) ORDER BY RANDOM() LIMIT $2",
                list(excluded_cluster_ids), limit,
            )
        else:
            rows = await self.pool.fetch(
                "SELECT * FROM mood_tags ORDER BY RANDOM() LIMIT $1", limit
            )
        return [dict(row) for row in rows]

    async def get_tag(self, tag_id: int) -> dict | None:
        """Return a single mood tag by ID."""
        row = await self.pool.fetchrow(
            "SELECT * FROM mood_tags WHERE id = $1", tag_id
        )
        if row is None:
            return None
        return dict(row)

    async def clear_mood_tags(self) -> None:
        """Delete all mood tags."""
        await self.pool.execute("DELETE FROM mood_tags")

    # ------------------------------------------------------------------
    # Job queue
    # ------------------------------------------------------------------

    async def create_job(self, data: JobCreate) -> Job:
        """Enqueue a new processing job and return the Job model."""
        await self.pool.execute(
            """
            INSERT INTO job_queue (
                id, track_id, mp3_key, artist_hint, title_hint,
                priority, status,
                locked_by, locked_at, data, result, error_message,
                current_step, progress, created_at, updated_at
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7,
                $8, $9, $10, $11, $12,
                $13, $14, $15, $16
            )
            """,
            data.id, data.track_id, data.mp3_key, data.artist_hint,
            data.title_hint,
            data.priority, data.status,
            None, None, json.dumps(data.data) if data.data else None,
            None, None,
            None, 0, _to_dt(data.created_at), _to_dt(data.updated_at),
        )
        job = await self.get_job(data.id)
        if job is None:
            raise RuntimeError(f"Job {data.id} not found after insert")
        return job

    async def get_job(self, job_id: str) -> Job | None:
        """Return a single job by primary key, or ``None`` if not found."""
        row = await self.pool.fetchrow(
            "SELECT * FROM job_queue WHERE id = $1", job_id
        )
        if row is None:
            return None
        return self._job_from_row(row)

    async def get_latest_completed_job_for_track(self, track_id: str) -> Job | None:
        """Return the newest completed job associated with a track."""
        row = await self.pool.fetchrow(
            """
            SELECT * FROM job_queue
            WHERE track_id = $1 AND status = $2
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            track_id,
            JobStatus.COMPLETED,
        )
        if row is None:
            return None
        return self._job_from_row(row)

    async def list_completed_jobs_for_track(
        self,
        track_id: str,
        limit: int = 25,
    ) -> list[Job]:
        """Return recent completed jobs associated with a track."""
        rows = await self.pool.fetch(
            """
            SELECT * FROM job_queue
            WHERE track_id = $1 AND status = $2
            ORDER BY updated_at DESC
            LIMIT $3
            """,
            track_id,
            JobStatus.COMPLETED,
            limit,
        )
        return [self._job_from_row(row) for row in rows]

    async def poll_and_lock(self, worker_id: str) -> Job | None:
        """Atomically find the highest-priority pending job and lock it.

        Uses FOR UPDATE SKIP LOCKED for safe multi-worker concurrency.
        """
        now = _now_dt()
        row = await self.pool.fetchrow(
            """
            UPDATE job_queue
            SET status = $1, locked_by = $2, locked_at = $3, updated_at = $3
            WHERE id = (
                SELECT id FROM job_queue
                WHERE status = $4
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING *
            """,
            JobStatus.RUNNING, worker_id, now, JobStatus.PENDING,
        )
        if row is None:
            return None
        return self._job_from_row(row)

    async def poll_pending(self, limit: int = 1) -> list[Job]:
        """Return the highest-priority pending jobs without locking them."""
        rows = await self.pool.fetch(
            """
            SELECT * FROM job_queue
            WHERE status = $1
            ORDER BY priority DESC, created_at ASC
            LIMIT $2
            """,
            JobStatus.PENDING, limit,
        )
        return [self._job_from_row(row) for row in rows]

    async def lock_job(self, job_id: str, worker_id: str) -> bool:
        """Attempt a pessimistic lock on a pending job."""
        now = _now_dt()
        result = await self.pool.execute(
            """
            UPDATE job_queue
            SET status = $1, locked_by = $2, locked_at = $3, updated_at = $3
            WHERE id = $4 AND status = $5
            """,
            JobStatus.RUNNING, worker_id, now, job_id, JobStatus.PENDING,
        )
        return result.endswith("1")

    async def complete_job(self, job_id: str, result: dict) -> None:
        """Mark a job as completed and store its result payload."""
        await self.pool.execute(
            """
            UPDATE job_queue
            SET status = $1, result = $2, updated_at = $3
            WHERE id = $4
            """,
            JobStatus.COMPLETED, json.dumps(result), _now_dt(), job_id,
        )

    async def fail_job_permanently(self, job_id: str, error: str) -> None:
        """Mark a job as failed and route it to DLQ semantics."""
        await self.pool.execute(
            """
            UPDATE job_queue
            SET status = $1, error_message = $2,
                locked_by = NULL, locked_at = NULL, updated_at = $3
            WHERE id = $4
            """,
            JobStatus.FAILED, error, _now_dt(), job_id,
        )

    async def reset_stale_running_jobs(self, worker_id: str) -> int:
        """Reset jobs left in 'running' state by this worker back to 'pending'."""
        result = await self.pool.execute(
            """
            UPDATE job_queue
            SET status = $1, locked_by = NULL, locked_at = NULL, updated_at = $2
            WHERE status = $3 AND locked_by = $4
            """,
            JobStatus.PENDING, _now_dt(), JobStatus.RUNNING, worker_id,
        )
        # asyncpg returns "UPDATE N"
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0

    async def get_active_upload_jobs(self) -> list[Job]:
        """Return pending/running jobs (user uploads or those with mp3_key)."""
        rows = await self.pool.fetch(
            """
            SELECT * FROM job_queue
            WHERE mp3_key IS NOT NULL
              AND status IN ($1, $2)
            ORDER BY created_at DESC
            """,
            JobStatus.PENDING, JobStatus.RUNNING,
        )
        return [self._job_from_row(row) for row in rows]

    async def find_stale_pending_jobs(
        self, older_than_seconds: int
    ) -> list[Job]:
        """Return pending upload jobs whose updated_at is older than the cutoff.

        Used by the backend's periodic sweeper to detect orphan jobs whose
        RMQ message was lost (e.g. queue recreated, broker volume reset,
        race between INSERT INTO job_queue and rmq.publish). Filters by
        mp3_key IS NOT NULL to skip non-upload jobs.
        """
        rows = await self.pool.fetch(
            """
            SELECT * FROM job_queue
            WHERE status = $1
              AND mp3_key IS NOT NULL
              AND updated_at < (now() - make_interval(secs => $2))
            ORDER BY created_at ASC
            """,
            JobStatus.PENDING, older_than_seconds,
        )
        return [self._job_from_row(row) for row in rows]

    async def mark_step(self, job_id: str, step: str, progress: int) -> None:
        """Update the current processing step and progress percentage."""
        await self.pool.execute(
            "UPDATE job_queue SET current_step = $1, progress = $2, updated_at = $3 WHERE id = $4",
            step, progress, _now_dt(), job_id,
        )

    async def update_job_data(self, job_id: str, new_data: dict) -> None:
        """Merge new_data into the job's data JSONB field."""
        await self.pool.execute(
            """
            UPDATE job_queue
            SET data = COALESCE(data, '{}'::jsonb) || $1::jsonb,
                updated_at = $2
            WHERE id = $3
            """,
            json.dumps(new_data), _now_dt(), job_id,
        )

    async def set_job_track_id(self, job_id: str, track_id: str) -> None:
        """Set the track_id on a job after track creation (finalisation)."""
        await self.pool.execute(
            "UPDATE job_queue SET track_id = $1, updated_at = $2 WHERE id = $3",
            track_id, _now_dt(), job_id,
        )

    def _job_from_row(self, row: asyncpg.Record) -> Job:
        """Build a Job model from a DB row, deserializing the JSON fields."""
        result: dict | None = None
        raw_result = row.get("result")
        if raw_result is not None:
            if isinstance(raw_result, str):
                result = json.loads(raw_result)
            else:
                result = raw_result

        data: dict | None = None
        raw_data = row.get("data")
        if raw_data is not None:
            if isinstance(raw_data, str):
                data = json.loads(raw_data)
            else:
                data = raw_data

        return Job(
            id=row["id"],
            track_id=row.get("track_id"),
            mp3_key=row.get("mp3_key"),
            artist_hint=row.get("artist_hint"),
            title_hint=row.get("title_hint"),
            priority=row.get("priority", 1),
            status=row["status"],
            locked_by=row.get("locked_by"),
            locked_at=_ts(row.get("locked_at")),
            data=data,
            result=result,
            error_message=row.get("error_message"),
            current_step=row.get("current_step"),
            progress=row.get("progress", 0),
            created_at=_ts(row["created_at"]),
            updated_at=_ts(row["updated_at"]),
        )

    # ------------------------------------------------------------------
    # API cost tracking
    # ------------------------------------------------------------------

    async def record_api_cost(
        self,
        track_id: str,
        service: str,
        cost_usd: float,
        tokens: int | None = None,
        duration_sec: float | None = None,
    ) -> None:
        """Record the cost of an API call for a track."""
        await self.pool.execute(
            """
            INSERT INTO api_costs (track_id, service, cost_usd, tokens, duration_sec)
            VALUES ($1, $2, $3, $4, $5)
            """,
            track_id, service, cost_usd, tokens, duration_sec,
        )

    async def get_monthly_costs(self) -> dict[str, float]:
        """Return total cost per service for the current month."""
        rows = await self.pool.fetch(
            """
            SELECT service, SUM(cost_usd) as total
            FROM api_costs
            WHERE created_at >= date_trunc('month', NOW())
            GROUP BY service
            """,
        )
        return {row["service"]: row["total"] for row in rows}
