"""Integration tests for the Queue API endpoints.

Tests cover:
    GET    /api/v1/sessions/{session_id}/queue
    POST   /api/v1/queue
    POST   /api/v1/queue/{entry_id}/skip
    POST   /api/v1/queue/{entry_id}/start
    POST   /api/v1/queue/{entry_id}/finish
    DELETE /api/v1/queue/{entry_id}

Each test class uses a shared ``queue_fixtures`` fixture that sets up:
- One active session
- Two participants (Alice, Bob)
- One "ready" track in the SQLite DB (inserted directly via SQLiteRepository)

Individual tests add queue entries as needed.

Run with:
    PYTHONPATH=backend ADMIN_SECRET=test-secret \\
        python -m pytest tests/test_api_queue.py -v
"""

from __future__ import annotations

import pathlib

import aiosqlite
import pytest
import pytest_asyncio

from karaoke_shared.models import TrackCreate
from karaoke_shared.repositories import SQLiteRepository


# ---------------------------------------------------------------------------
# Per-test fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def queue_fixtures(client, app_db):
    """Set up a session with two participants and a ready track.

    Returns a dict with:
        session_id, participant_id (Alice), participant2_id (Bob),
        track_id, repo (SQLiteRepository for direct DB access)
    """
    repo = SQLiteRepository(app_db)

    # Insert a ready track directly (no track upload endpoint yet)
    track = await repo.create_track(
        TrackCreate(
            artist="Test Artist",
            title="Test Track",
            source="catalog",
            status="ready",
            clip_path="/media/test.mp4",
            duration_sec=210,
        )
    )

    # Create a session via the API
    r = await client.post("/api/v1/sessions", json={"room_id": "room-queue-1"})
    assert r.status_code == 201
    session_id = r.json()["id"]

    # Add two participants
    r1 = await client.post(
        f"/api/v1/sessions/{session_id}/participants", json={"name": "Alice"}
    )
    assert r1.status_code == 201
    participant_id = r1.json()["id"]

    r2 = await client.post(
        f"/api/v1/sessions/{session_id}/participants", json={"name": "Bob"}
    )
    assert r2.status_code == 201
    participant2_id = r2.json()["id"]

    return {
        "session_id": session_id,
        "participant_id": participant_id,
        "participant2_id": participant2_id,
        "track_id": track.id,
        "repo": repo,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _add_entry(client, session_id: str, participant_id: str, track_id: str) -> dict:
    """POST /api/v1/queue and return the response body."""
    r = await client.post(
        "/api/v1/queue",
        json={
            "session_id": session_id,
            "participant_id": participant_id,
            "track_id": track_id,
        },
    )
    assert r.status_code == 201, f"add_entry failed: {r.status_code} {r.text}"
    return r.json()


# ---------------------------------------------------------------------------
# POST /api/v1/queue
# ---------------------------------------------------------------------------


class TestAddToQueue:
    async def test_returns_201_with_queued_entry(self, client, queue_fixtures):
        """Adding a track to the queue returns 201 and a 'queued' entry."""
        fx = queue_fixtures
        response = await client.post(
            "/api/v1/queue",
            json={
                "session_id": fx["session_id"],
                "participant_id": fx["participant_id"],
                "track_id": fx["track_id"],
            },
        )

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "queued"
        assert body["session_id"] == fx["session_id"]
        assert body["participant_id"] == fx["participant_id"]
        assert body["track_id"] == fx["track_id"]
        assert "id" in body
        assert body["id"] != ""
        assert body["order_position"] == 1

    async def test_second_entry_gets_higher_position(self, client, queue_fixtures):
        """A second queue entry gets order_position 2."""
        fx = queue_fixtures
        e1 = await _add_entry(
            client, fx["session_id"], fx["participant_id"], fx["track_id"]
        )
        e2 = await _add_entry(
            client, fx["session_id"], fx["participant2_id"], fx["track_id"]
        )

        assert e1["order_position"] == 1
        assert e2["order_position"] == 2

    async def test_session_not_found_returns_404(self, client, queue_fixtures):
        """Adding to a non-existent session returns 404."""
        fx = queue_fixtures
        response = await client.post(
            "/api/v1/queue",
            json={
                "session_id": "no-such-session",
                "participant_id": fx["participant_id"],
                "track_id": fx["track_id"],
            },
        )

        assert response.status_code == 404

    async def test_terminated_session_returns_409(self, client, queue_fixtures):
        """Adding to a terminated session returns 409 Conflict."""
        fx = queue_fixtures
        await client.delete(
            f"/api/v1/sessions/{fx['session_id']}",
            headers={"X-Admin-Secret": "test-secret"},
        )

        response = await client.post(
            "/api/v1/queue",
            json={
                "session_id": fx["session_id"],
                "participant_id": fx["participant_id"],
                "track_id": fx["track_id"],
            },
        )

        assert response.status_code == 409


# ---------------------------------------------------------------------------
# GET /api/v1/sessions/{session_id}/queue
# ---------------------------------------------------------------------------


class TestGetQueue:
    async def test_empty_queue_returns_null_current_and_empty_upcoming(
        self, client, queue_fixtures
    ):
        """A session with no queue entries returns current=null, upcoming=[]."""
        fx = queue_fixtures
        response = await client.get(f"/api/v1/sessions/{fx['session_id']}/queue")

        assert response.status_code == 200
        body = response.json()
        assert body["current"] is None
        assert body["upcoming"] == []

    async def test_single_entry_is_current_with_no_upcoming(
        self, client, queue_fixtures
    ):
        """One queue entry is returned as current with empty upcoming list."""
        fx = queue_fixtures
        entry = await _add_entry(
            client, fx["session_id"], fx["participant_id"], fx["track_id"]
        )

        response = await client.get(f"/api/v1/sessions/{fx['session_id']}/queue")

        assert response.status_code == 200
        body = response.json()
        assert body["current"] is not None
        assert body["current"]["id"] == entry["id"]
        assert body["upcoming"] == []

    async def test_three_entries_splits_into_current_and_upcoming(
        self, client, queue_fixtures
    ):
        """With 3 entries, current is the first and upcoming contains the rest."""
        fx = queue_fixtures
        e1 = await _add_entry(
            client, fx["session_id"], fx["participant_id"], fx["track_id"]
        )
        e2 = await _add_entry(
            client, fx["session_id"], fx["participant2_id"], fx["track_id"]
        )
        e3 = await _add_entry(
            client, fx["session_id"], fx["participant_id"], fx["track_id"]
        )

        response = await client.get(f"/api/v1/sessions/{fx['session_id']}/queue")

        assert response.status_code == 200
        body = response.json()
        assert body["current"]["id"] == e1["id"]
        assert len(body["upcoming"]) == 2
        upcoming_ids = [u["id"] for u in body["upcoming"]]
        assert e2["id"] in upcoming_ids
        assert e3["id"] in upcoming_ids

    async def test_queue_entries_include_participant_and_track(
        self, client, queue_fixtures
    ):
        """Queue response enriches entries with participant and track details."""
        fx = queue_fixtures
        await _add_entry(
            client, fx["session_id"], fx["participant_id"], fx["track_id"]
        )

        response = await client.get(f"/api/v1/sessions/{fx['session_id']}/queue")
        body = response.json()
        current = body["current"]

        assert current["participant"] is not None
        assert current["participant"]["display_name"] == "Alice"
        assert current["track"] is not None
        assert current["track"]["artist"] == "Test Artist"
        assert current["track"]["title"] == "Test Track"


# ---------------------------------------------------------------------------
# POST /api/v1/queue/{entry_id}/skip
# ---------------------------------------------------------------------------


class TestSkipTurn:
    async def test_skip_returns_new_entry_at_end_of_queue(
        self, client, queue_fixtures
    ):
        """Skipping an entry marks it skipped and creates a new entry at end."""
        fx = queue_fixtures
        e1 = await _add_entry(
            client, fx["session_id"], fx["participant_id"], fx["track_id"]
        )
        e2 = await _add_entry(
            client, fx["session_id"], fx["participant2_id"], fx["track_id"]
        )

        response = await client.post(f"/api/v1/queue/{e1['id']}/skip")

        assert response.status_code == 200
        new_entry = response.json()
        # The new entry should have a different ID and higher position
        assert new_entry["id"] != e1["id"]
        assert new_entry["order_position"] > e2["order_position"]
        assert new_entry["participant_id"] == fx["participant_id"]
        assert new_entry["track_id"] == fx["track_id"]
        assert new_entry["status"] == "queued"

    async def test_skip_removes_original_from_active_queue(
        self, client, queue_fixtures
    ):
        """After skipping, the original entry is no longer 'queued' or 'playing'."""
        fx = queue_fixtures
        e1 = await _add_entry(
            client, fx["session_id"], fx["participant_id"], fx["track_id"]
        )

        await client.post(f"/api/v1/queue/{e1['id']}/skip")

        # Get the queue and verify the original entry_id is gone
        q_resp = await client.get(f"/api/v1/sessions/{fx['session_id']}/queue")
        body = q_resp.json()
        active_ids = []
        if body["current"]:
            active_ids.append(body["current"]["id"])
        active_ids.extend(u["id"] for u in body["upcoming"])

        assert e1["id"] not in active_ids

    async def test_skip_nonexistent_entry_returns_404(self, client, queue_fixtures):
        """Skipping a non-existent entry returns 404."""
        response = await client.post("/api/v1/queue/nonexistent-entry/skip")

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/queue/{entry_id}/start
# ---------------------------------------------------------------------------


class TestStartPlaying:
    async def test_start_nonexistent_entry_returns_404(self, client, queue_fixtures):
        """Starting a non-existent entry returns 404."""
        response = await client.post("/api/v1/queue/ghost-entry-id/start")

        assert response.status_code == 404

    async def test_started_entry_appears_as_current_in_queue(
        self, client, queue_fixtures
    ):
        """After starting, the entry status changes to 'playing'."""
        fx = queue_fixtures
        e1 = await _add_entry(
            client, fx["session_id"], fx["participant_id"], fx["track_id"]
        )
        await _add_entry(
            client, fx["session_id"], fx["participant2_id"], fx["track_id"]
        )

        await client.post(f"/api/v1/queue/{e1['id']}/start")

        q_resp = await client.get(f"/api/v1/sessions/{fx['session_id']}/queue")
        current = q_resp.json()["current"]
        assert current["id"] == e1["id"]
        assert current["status"] == "playing"
        assert current["started_at"] is not None


# ---------------------------------------------------------------------------
# POST /api/v1/queue/{entry_id}/finish
# ---------------------------------------------------------------------------


class TestFinishPlaying:
    async def test_finish_returns_next_participant_and_entry(
        self, client, queue_fixtures
    ):
        """Finishing a playing entry returns the next participant and entry ID."""
        fx = queue_fixtures
        e1 = await _add_entry(
            client, fx["session_id"], fx["participant_id"], fx["track_id"]
        )
        e2 = await _add_entry(
            client, fx["session_id"], fx["participant2_id"], fx["track_id"]
        )

        await client.post(f"/api/v1/queue/{e1['id']}/start")
        response = await client.post(f"/api/v1/queue/{e1['id']}/finish")

        assert response.status_code == 200
        body = response.json()
        assert body["next_entry_id"] == e2["id"]
        assert body["next_participant"] is not None
        assert body["next_participant"]["display_name"] == "Bob"

    async def test_finish_last_entry_returns_null_next(self, client, queue_fixtures):
        """Finishing the only entry in the queue returns next_participant=null."""
        fx = queue_fixtures
        entry = await _add_entry(
            client, fx["session_id"], fx["participant_id"], fx["track_id"]
        )

        await client.post(f"/api/v1/queue/{entry['id']}/start")
        response = await client.post(f"/api/v1/queue/{entry['id']}/finish")

        assert response.status_code == 200
        body = response.json()
        assert body["next_participant"] is None
        assert body["next_entry_id"] is None

    async def test_finish_increments_track_play_count(self, client, queue_fixtures):
        """Finishing playback increments the track's play_count by 1."""
        fx = queue_fixtures
        entry = await _add_entry(
            client, fx["session_id"], fx["participant_id"], fx["track_id"]
        )

        await client.post(f"/api/v1/queue/{entry['id']}/start")
        await client.post(f"/api/v1/queue/{entry['id']}/finish")

        track = await fx["repo"].get_track(fx["track_id"])
        assert track is not None
        assert track.play_count == 1

    async def test_finish_increments_participant_tracks_played(
        self, client, queue_fixtures
    ):
        """Finishing playback increments the participant's tracks_played counter."""
        fx = queue_fixtures
        entry = await _add_entry(
            client, fx["session_id"], fx["participant_id"], fx["track_id"]
        )

        await client.post(f"/api/v1/queue/{entry['id']}/start")
        await client.post(f"/api/v1/queue/{entry['id']}/finish")

        participant = await fx["repo"].get_participant(fx["participant_id"])
        assert participant is not None
        assert participant.tracks_played == 1

    async def test_finish_nonexistent_entry_returns_404(self, client, queue_fixtures):
        """Finishing a non-existent entry returns 404."""
        response = await client.post("/api/v1/queue/ghost-entry-id/finish")

        assert response.status_code == 404

    async def test_finish_removes_entry_from_active_queue(
        self, client, queue_fixtures
    ):
        """After finishing, the done entry no longer appears in queue output."""
        fx = queue_fixtures
        entry = await _add_entry(
            client, fx["session_id"], fx["participant_id"], fx["track_id"]
        )

        await client.post(f"/api/v1/queue/{entry['id']}/start")
        await client.post(f"/api/v1/queue/{entry['id']}/finish")

        q_resp = await client.get(f"/api/v1/sessions/{fx['session_id']}/queue")
        body = q_resp.json()
        assert body["current"] is None
        assert body["upcoming"] == []


# ---------------------------------------------------------------------------
# DELETE /api/v1/queue/{entry_id}
# ---------------------------------------------------------------------------


class TestDeleteQueueEntry:
    async def test_delete_returns_204(self, client, queue_fixtures):
        """Deleting an existing queue entry returns 204 No Content."""
        fx = queue_fixtures
        entry = await _add_entry(
            client, fx["session_id"], fx["participant_id"], fx["track_id"]
        )

        response = await client.delete(f"/api/v1/queue/{entry['id']}")

        assert response.status_code == 204

    async def test_deleted_entry_absent_from_queue(self, client, queue_fixtures):
        """After DELETE, the entry no longer appears in the session queue."""
        fx = queue_fixtures
        entry = await _add_entry(
            client, fx["session_id"], fx["participant_id"], fx["track_id"]
        )

        await client.delete(f"/api/v1/queue/{entry['id']}")

        q_resp = await client.get(f"/api/v1/sessions/{fx['session_id']}/queue")
        body = q_resp.json()
        assert body["current"] is None
        assert body["upcoming"] == []

    async def test_delete_nonexistent_entry_returns_404(self, client, queue_fixtures):
        """Deleting a non-existent entry returns 404."""
        response = await client.delete("/api/v1/queue/no-such-entry-id")

        assert response.status_code == 404

    async def test_delete_one_entry_leaves_others_intact(
        self, client, queue_fixtures
    ):
        """Deleting one entry from a multi-entry queue leaves the rest intact."""
        fx = queue_fixtures
        e1 = await _add_entry(
            client, fx["session_id"], fx["participant_id"], fx["track_id"]
        )
        e2 = await _add_entry(
            client, fx["session_id"], fx["participant2_id"], fx["track_id"]
        )

        await client.delete(f"/api/v1/queue/{e1['id']}")

        q_resp = await client.get(f"/api/v1/sessions/{fx['session_id']}/queue")
        body = q_resp.json()
        assert body["current"]["id"] == e2["id"]
        assert body["upcoming"] == []
