"""Unit tests for all Pydantic models in karaoke_shared.models.

Coverage:
- Default value generation (UUID ids, ISO8601 timestamps)
- Required field validation (missing required field raises ValidationError)
- Serialization round-trips (model -> dict -> model)
- TrackUpdate partial update semantics
- SyllableTiming structure
- RecommendationStrategy enum values
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

import pytest
from pydantic import ValidationError

from karaoke_shared.models import (
    Job,
    JobCreate,
    JobUpdate,
    Participant,
    ParticipantCreate,
    PlayHistoryCreate,
    PlayHistoryEntry,
    QueueEntry,
    QueueEntryCreate,
    RecommendationResponse,
    RecommendationStrategy,
    RecommendedTrackItem,
    Session,
    SessionCreate,
    SyllableTiming,
    Track,
    TrackCreate,
    TrackUpdate,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ISO8601_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def _is_iso8601(value: str) -> bool:
    return bool(_ISO8601_RE.match(value))


# ---------------------------------------------------------------------------
# SyllableTiming
# ---------------------------------------------------------------------------


class TestSyllableTiming:
    def test_valid_syllable_timing(self):
        st = SyllableTiming(syllable="До", start=0.5, end=0.8)
        assert st.syllable == "До"
        assert st.start == 0.5
        assert st.end == 0.8

    def test_syllable_timing_round_trip(self):
        original = SyllableTiming(syllable="re", start=1.0, end=1.5)
        data = original.model_dump()
        restored = SyllableTiming(**data)
        assert restored == original

    @pytest.mark.parametrize(
        "missing_field,fields",
        [
            ("syllable", {"start": 0.0, "end": 0.5}),
            ("start", {"syllable": "la", "end": 0.5}),
            ("end", {"syllable": "la", "start": 0.0}),
        ],
    )
    def test_missing_required_field_raises(self, missing_field, fields):
        with pytest.raises(ValidationError):
            SyllableTiming(**fields)


# ---------------------------------------------------------------------------
# Track / TrackCreate / TrackUpdate
# ---------------------------------------------------------------------------


class TestTrackCreate:
    def test_defaults_generated(self):
        tc = TrackCreate(artist="Кино", title="Группа крови", source="catalog")
        assert _is_uuid(tc.id), f"id should be a UUID, got {tc.id!r}"
        assert _is_iso8601(tc.created_at), f"created_at not ISO8601: {tc.created_at!r}"
        assert _is_iso8601(tc.updated_at), f"updated_at not ISO8601: {tc.updated_at!r}"
        assert tc.status == "pending"
        assert tc.play_count == 0
        assert tc.qdrant_synced == 0

    def test_each_instance_gets_unique_id(self):
        a = TrackCreate(artist="A", title="A", source="catalog")
        b = TrackCreate(artist="B", title="B", source="catalog")
        assert a.id != b.id

    def test_missing_artist_raises(self):
        with pytest.raises(ValidationError):
            TrackCreate(title="T", source="catalog")

    def test_missing_title_raises(self):
        with pytest.raises(ValidationError):
            TrackCreate(artist="A", source="catalog")

    def test_missing_source_raises(self):
        with pytest.raises(ValidationError):
            TrackCreate(artist="A", title="T")

    def test_optional_fields_default_none(self):
        tc = TrackCreate(artist="A", title="T", source="catalog")
        assert tc.duration_sec is None
        assert tc.mp3_path is None
        assert tc.instrumental_path is None
        assert tc.clip_path is None
        assert tc.lyrics_text is None
        assert tc.syllable_timings is None
        assert tc.language is None

    def test_syllable_timings_stored(self):
        timings = [SyllableTiming(syllable="la", start=0.0, end=0.5)]
        tc = TrackCreate(
            artist="A",
            title="T",
            source="catalog",
            syllable_timings=timings,
        )
        assert len(tc.syllable_timings) == 1
        assert tc.syllable_timings[0].syllable == "la"

    def test_round_trip(self):
        original = TrackCreate(
            artist="Ария",
            title="Герой асфальта",
            source="catalog",
            language="ru",
            duration_sec=240,
        )
        data = original.model_dump()
        restored = TrackCreate(**data)
        assert restored.artist == original.artist
        assert restored.title == original.title
        assert restored.id == original.id
        assert restored.created_at == original.created_at


class TestTrack:
    def test_full_model_construction(self):
        from datetime import timezone

        now = datetime.now(timezone.utc).isoformat()
        track = Track(
            id=str(uuid.uuid4()),
            artist="Zemfira",
            title="Искала",
            source="catalog",
            created_at=now,
            updated_at=now,
        )
        assert track.status == "pending"
        assert track.play_count == 0
        assert track.qdrant_synced == 0

    def test_missing_id_raises(self):
        from datetime import timezone

        now = datetime.now(timezone.utc).isoformat()
        with pytest.raises(ValidationError):
            Track(artist="A", title="T", source="catalog", created_at=now, updated_at=now)

    def test_missing_created_at_raises(self):
        with pytest.raises(ValidationError):
            Track(
                id=str(uuid.uuid4()),
                artist="A",
                title="T",
                source="catalog",
                updated_at="2024-01-01T00:00:00",
            )

    def test_round_trip(self):
        from datetime import timezone

        now = datetime.now(timezone.utc).isoformat()
        original = Track(
            id=str(uuid.uuid4()),
            artist="Ленинград",
            title="Дачники",
            source="user_upload",
            status="ready",
            play_count=5,
            created_at=now,
            updated_at=now,
        )
        data = original.model_dump()
        restored = Track(**data)
        assert restored == original


class TestTrackUpdate:
    def test_all_fields_default_none_except_updated_at(self):
        tu = TrackUpdate()
        assert tu.artist is None
        assert tu.title is None
        assert tu.duration_sec is None
        assert tu.mp3_path is None
        assert tu.instrumental_path is None
        assert tu.clip_path is None
        assert tu.lyrics_text is None
        assert tu.syllable_timings is None
        assert tu.language is None
        assert tu.source is None
        assert tu.status is None
        assert tu.error_message is None
        assert tu.play_count is None
        assert tu.qdrant_synced is None
        # updated_at should be auto-set
        assert _is_iso8601(tu.updated_at)

    def test_partial_update_only_set_fields_non_none(self):
        tu = TrackUpdate(status="ready", play_count=3)
        assert tu.status == "ready"
        assert tu.play_count == 3
        # All others remain None
        assert tu.artist is None
        assert tu.title is None

    def test_updated_at_auto_generated(self):
        tu = TrackUpdate(status="processing")
        assert _is_iso8601(tu.updated_at)

    def test_round_trip(self):
        original = TrackUpdate(status="ready", language="en")
        data = original.model_dump()
        restored = TrackUpdate(**data)
        assert restored.status == original.status
        assert restored.language == original.language
        assert restored.updated_at == original.updated_at


# ---------------------------------------------------------------------------
# Session / SessionCreate
# ---------------------------------------------------------------------------


class TestSessionCreate:
    def test_defaults_generated(self):
        sc = SessionCreate(room_id="room-1")
        assert _is_uuid(sc.id)
        assert _is_iso8601(sc.created_at)
        assert sc.status == "active"

    def test_each_instance_unique_id(self):
        a = SessionCreate(room_id="room-1")
        b = SessionCreate(room_id="room-1")
        assert a.id != b.id

    def test_missing_room_id_raises(self):
        with pytest.raises(ValidationError):
            SessionCreate()

    def test_round_trip(self):
        original = SessionCreate(room_id="room-42")
        data = original.model_dump()
        restored = SessionCreate(**data)
        assert restored.id == original.id
        assert restored.room_id == original.room_id
        assert restored.created_at == original.created_at


class TestSession:
    def test_missing_required_fields_raises(self):
        with pytest.raises(ValidationError):
            Session(room_id="r", status="active", created_at="2024-01-01T00:00:00")

    def test_terminated_at_optional(self):
        s = Session(
            id=str(uuid.uuid4()),
            room_id="r",
            status="active",
            created_at="2024-01-01T00:00:00",
        )
        assert s.terminated_at is None

    def test_round_trip(self):
        original = Session(
            id=str(uuid.uuid4()),
            room_id="room-7",
            status="terminated",
            created_at="2024-01-01T00:00:00",
            terminated_at="2024-01-01T01:00:00",
        )
        data = original.model_dump()
        restored = Session(**data)
        assert restored == original


# ---------------------------------------------------------------------------
# Participant / ParticipantCreate
# ---------------------------------------------------------------------------


class TestParticipantCreate:
    def test_defaults_generated(self):
        pc = ParticipantCreate(session_id="s1", display_name="Alice")
        assert _is_uuid(pc.id)
        assert _is_iso8601(pc.created_at)
        assert pc.tracks_played == 0

    def test_missing_session_id_raises(self):
        with pytest.raises(ValidationError):
            ParticipantCreate(display_name="Bob")

    def test_missing_display_name_raises(self):
        with pytest.raises(ValidationError):
            ParticipantCreate(session_id="s1")

    def test_round_trip(self):
        original = ParticipantCreate(session_id="s1", display_name="Charlie")
        data = original.model_dump()
        restored = ParticipantCreate(**data)
        assert restored.id == original.id
        assert restored.display_name == original.display_name


class TestParticipant:
    def test_portrait_vector_optional(self):
        p = Participant(
            id=str(uuid.uuid4()),
            session_id="s1",
            display_name="Dave",
            created_at="2024-01-01T00:00:00",
        )
        assert p.portrait_vector is None
        assert p.tracks_played == 0

    def test_round_trip(self):
        original = Participant(
            id=str(uuid.uuid4()),
            session_id="s1",
            display_name="Eve",
            portrait_vector=[0.1, 0.2, 0.3],
            tracks_played=5,
            created_at="2024-01-01T00:00:00",
        )
        data = original.model_dump()
        restored = Participant(**data)
        assert restored == original


# ---------------------------------------------------------------------------
# QueueEntry / QueueEntryCreate
# ---------------------------------------------------------------------------


class TestQueueEntryCreate:
    def test_defaults_generated(self):
        qec = QueueEntryCreate(
            session_id="s1",
            participant_id="p1",
            track_id="t1",
        )
        assert _is_uuid(qec.id)
        assert _is_iso8601(qec.added_at)
        assert qec.status == "queued"

    def test_missing_session_id_raises(self):
        with pytest.raises(ValidationError):
            QueueEntryCreate(participant_id="p1", track_id="t1")

    def test_missing_participant_id_raises(self):
        with pytest.raises(ValidationError):
            QueueEntryCreate(session_id="s1", track_id="t1")

    def test_missing_track_id_raises(self):
        with pytest.raises(ValidationError):
            QueueEntryCreate(session_id="s1", participant_id="p1")

    def test_round_trip(self):
        original = QueueEntryCreate(
            session_id="s1", participant_id="p1", track_id="t1"
        )
        data = original.model_dump()
        restored = QueueEntryCreate(**data)
        assert restored.id == original.id
        assert restored.added_at == original.added_at


class TestQueueEntry:
    def test_timestamps_optional(self):
        qe = QueueEntry(
            id=str(uuid.uuid4()),
            session_id="s1",
            participant_id="p1",
            track_id="t1",
            order_position=1,
            status="queued",
            added_at="2024-01-01T00:00:00",
        )
        assert qe.started_at is None
        assert qe.finished_at is None

    def test_round_trip(self):
        original = QueueEntry(
            id=str(uuid.uuid4()),
            session_id="s1",
            participant_id="p1",
            track_id="t1",
            order_position=2,
            status="playing",
            added_at="2024-01-01T00:00:00",
            started_at="2024-01-01T00:01:00",
        )
        data = original.model_dump()
        restored = QueueEntry(**data)
        assert restored == original


# ---------------------------------------------------------------------------
# PlayHistoryEntry / PlayHistoryCreate
# ---------------------------------------------------------------------------


class TestPlayHistoryCreate:
    def test_defaults_generated(self):
        phc = PlayHistoryCreate(
            session_id="s1", participant_id="p1", track_id="t1"
        )
        assert _is_uuid(phc.id)
        assert _is_iso8601(phc.played_at)
        assert phc.completed == 0

    def test_missing_required_raises(self):
        with pytest.raises(ValidationError):
            PlayHistoryCreate(session_id="s1", participant_id="p1")

    def test_round_trip(self):
        original = PlayHistoryCreate(
            session_id="s1", participant_id="p1", track_id="t1", completed=1
        )
        data = original.model_dump()
        restored = PlayHistoryCreate(**data)
        assert restored.id == original.id
        assert restored.completed == 1


# ---------------------------------------------------------------------------
# Job / JobCreate / JobUpdate
# ---------------------------------------------------------------------------


class TestJobCreate:
    def test_defaults_generated(self):
        jc = JobCreate(track_id="t1")
        assert _is_uuid(jc.id)
        assert _is_iso8601(jc.created_at)
        assert _is_iso8601(jc.updated_at)
        assert jc.status == "pending"
        assert jc.attempts == 0
        assert jc.max_attempts == 3
        assert jc.priority == 1

    def test_missing_track_id_raises(self):
        with pytest.raises(ValidationError):
            JobCreate()

    def test_round_trip(self):
        original = JobCreate(track_id="t1", priority=5)
        data = original.model_dump()
        restored = JobCreate(**data)
        assert restored.id == original.id
        assert restored.priority == 5


class TestJobUpdate:
    def test_all_fields_default_none_except_updated_at(self):
        ju = JobUpdate()
        assert ju.status is None
        assert ju.attempts is None
        assert ju.locked_by is None
        assert ju.locked_at is None
        assert ju.result is None
        assert ju.error_message is None
        assert _is_iso8601(ju.updated_at)

    def test_partial_fields_set(self):
        ju = JobUpdate(status="running", locked_by="worker-1")
        assert ju.status == "running"
        assert ju.locked_by == "worker-1"
        assert ju.attempts is None

    def test_round_trip(self):
        original = JobUpdate(status="completed", result={"output": "ok"})
        data = original.model_dump()
        restored = JobUpdate(**data)
        assert restored.status == original.status
        assert restored.result == original.result
        assert restored.updated_at == original.updated_at


class TestJob:
    def test_nullable_fields_default(self):
        from datetime import timezone

        now = datetime.now(timezone.utc).isoformat()
        job = Job(
            id=str(uuid.uuid4()),
            track_id="t1",
            status="pending",
            created_at=now,
            updated_at=now,
        )
        assert job.locked_by is None
        assert job.locked_at is None
        assert job.result is None
        assert job.error_message is None
        assert job.priority == 1
        assert job.attempts == 0
        assert job.max_attempts == 3

    def test_round_trip(self):
        from datetime import timezone

        now = datetime.now(timezone.utc).isoformat()
        original = Job(
            id=str(uuid.uuid4()),
            track_id="t1",
            status="completed",
            result={"key": "value"},
            created_at=now,
            updated_at=now,
        )
        data = original.model_dump()
        restored = Job(**data)
        assert restored == original


# ---------------------------------------------------------------------------
# RecommendationStrategy enum
# ---------------------------------------------------------------------------


class TestRecommendationStrategy:
    @pytest.mark.parametrize(
        "name, expected_value",
        [
            ("POPULAR", "popular"),
            ("LAST", "last"),
            ("LAST_TWO_AVG", "last_two_avg"),
            ("SESSION_AVG", "session_avg"),
        ],
    )
    def test_enum_values(self, name, expected_value):
        member = RecommendationStrategy[name]
        assert member.value == expected_value

    def test_all_members_present(self):
        values = {m.value for m in RecommendationStrategy}
        assert values == {"popular", "last", "last_two_avg", "session_avg"}

    def test_is_str_enum(self):
        assert isinstance(RecommendationStrategy.POPULAR, str)
        assert RecommendationStrategy.POPULAR == "popular"


# ---------------------------------------------------------------------------
# RecommendationResponse
# ---------------------------------------------------------------------------


class TestRecommendationResponse:
    def test_construction(self):
        item = RecommendedTrackItem(
            id=str(uuid.uuid4()),
            artist="A",
            title="T",
            duration_sec=200,
            similarity_score=0.85,
        )
        resp = RecommendationResponse(
            tracks=[item],
            strategy=RecommendationStrategy.POPULAR,
        )
        assert len(resp.tracks) == 1
        assert resp.strategy == RecommendationStrategy.POPULAR
        assert resp.tracks[0].similarity_score == 0.85

    def test_empty_tracks_list(self):
        resp = RecommendationResponse(
            tracks=[], strategy=RecommendationStrategy.SESSION_AVG
        )
        assert resp.tracks == []

    def test_missing_strategy_raises(self):
        with pytest.raises(ValidationError):
            RecommendationResponse(tracks=[])
