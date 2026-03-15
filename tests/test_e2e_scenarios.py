"""End-to-end integration tests for the Karaoke Club application.

Tests cover complete user journeys and edge cases across the full API surface:

1. Full E2E user journey (session -> participants -> search -> queue ->
   start -> finish -> skip -> recommendations -> terminate)
2. Edge case tests (bad uploads, empty catalog, double-finish, double-start,
   unknown IDs, zero-participant sessions)
3. Stress test (5 participants, 20-track queue with rotation correctness)

All tests use the ``client`` and ``app_db`` fixtures from conftest.py.
Each test gets a fresh in-memory SQLite database — no shared state.

Run with:
    PYTHONPATH=backend ADMIN_SECRET=test-secret \\
        python -m pytest tests/test_e2e_scenarios.py -v
"""

from __future__ import annotations

import io

import pytest

from karaoke_shared.models import TrackCreate
from karaoke_shared.repositories import SQLiteRepository


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


async def _create_track(
    repo: SQLiteRepository,
    artist: str = "Test Artist",
    title: str = "Test Song",
    clip_path: str = "/media/test.mp4",
    duration_sec: int = 210,
) -> str:
    """Insert a ready track into the DB and return its ID."""
    track = await repo.create_track(
        TrackCreate(
            artist=artist,
            title=title,
            source="catalog",
            status="ready",
            clip_path=clip_path,
            duration_sec=duration_sec,
        )
    )
    return track.id


async def _create_session(client, room_id: str = "e2e-room") -> str:
    """POST /api/v1/sessions and return the session ID."""
    r = await client.post("/api/v1/sessions", json={"room_id": room_id})
    assert r.status_code == 201, f"create_session failed: {r.status_code} {r.text}"
    return r.json()["id"]


async def _add_participant(client, session_id: str, name: str) -> str:
    """POST /api/v1/sessions/{id}/participants and return the participant ID."""
    r = await client.post(
        f"/api/v1/sessions/{session_id}/participants", json={"name": name}
    )
    assert r.status_code == 201, f"add_participant failed: {r.status_code} {r.text}"
    return r.json()["id"]


async def _add_to_queue(
    client, session_id: str, participant_id: str, track_id: str
) -> dict:
    """POST /api/v1/queue and return the full response body."""
    r = await client.post(
        "/api/v1/queue",
        json={
            "session_id": session_id,
            "participant_id": participant_id,
            "track_id": track_id,
        },
    )
    assert r.status_code == 201, f"add_to_queue failed: {r.status_code} {r.text}"
    return r.json()


# ---------------------------------------------------------------------------
# 2. Edge case tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and error conditions across all API endpoints."""

    # ------------------------------------------------------------------
    # Invalid file upload handling
    # ------------------------------------------------------------------

    async def test_upload_rejects_ogg_file(self, client, monkeypatch, tmp_path):
        """Uploading an .ogg file (wrong extension) returns 422."""
        import app.config as config_module

        monkeypatch.setattr(config_module.settings, "media_root", str(tmp_path))

        response = await client.post(
            "/api/v1/tracks/upload",
            files={"file": ("song.ogg", io.BytesIO(b"OGG data"), "audio/ogg")},
        )
        assert response.status_code == 422
        assert "mp3" in response.json()["detail"].lower()

    async def test_upload_rejects_mp4_content_type(self, client, monkeypatch, tmp_path):
        """Uploading a file with video/mp4 content-type returns 422."""
        import app.config as config_module

        monkeypatch.setattr(config_module.settings, "media_root", str(tmp_path))

        response = await client.post(
            "/api/v1/tracks/upload",
            files={"file": ("song.mp3", io.BytesIO(b"not audio"), "video/mp4")},
        )
        assert response.status_code == 422

    async def test_upload_rejects_file_exceeding_50mb(self, client, monkeypatch, tmp_path):
        """Uploading a file over 50 MB returns 413 Request Entity Too Large."""
        import app.config as config_module

        monkeypatch.setattr(config_module.settings, "media_root", str(tmp_path))

        big_content = b"\x00" * (51 * 1024 * 1024)
        response = await client.post(
            "/api/v1/tracks/upload",
            files={"file": ("big.mp3", io.BytesIO(big_content), "audio/mpeg")},
        )
        assert response.status_code == 413

    async def test_upload_rejects_wav_extension(self, client, monkeypatch, tmp_path):
        """Uploading a .wav file (wrong extension) returns 422."""
        import app.config as config_module

        monkeypatch.setattr(config_module.settings, "media_root", str(tmp_path))

        response = await client.post(
            "/api/v1/tracks/upload",
            files={"file": ("song.wav", io.BytesIO(b"RIFF"), "audio/wav")},
        )
        assert response.status_code == 422

    # ------------------------------------------------------------------
    # Empty catalog — recommendations return empty
    # ------------------------------------------------------------------

    async def test_recommendations_empty_catalog_returns_popular_strategy_with_no_tracks(
        self, client, app_db
    ):
        """With an empty track catalog, recommendations use popular strategy
        and return zero tracks."""
        session_id = await _create_session(client, "empty-catalog-room")
        participant_id = await _add_participant(client, session_id, "Иван")

        rec_resp = await client.get(
            "/api/v1/recommendations",
            params={"participant_id": participant_id, "session_id": session_id},
        )
        assert rec_resp.status_code == 200
        data = rec_resp.json()
        assert data["strategy"] == "popular"
        assert data["tracks"] == []

    # ------------------------------------------------------------------
    # Session with 0 participants — queue operations work
    # ------------------------------------------------------------------

    async def test_queue_operations_with_zero_participants_session(
        self, client, app_db
    ):
        """A session with no participants can still have queue entries added
        (participant_id is not validated by the queue endpoint)."""
        repo = SQLiteRepository(app_db)
        track_id = await _create_track(repo, artist="Solo", title="Song")
        session_id = await _create_session(client, "zero-participants-room")

        # We use a made-up participant_id — queue endpoint does not validate it
        fake_participant_id = "00000000-0000-0000-0000-000000000001"

        r = await client.post(
            "/api/v1/queue",
            json={
                "session_id": session_id,
                "participant_id": fake_participant_id,
                "track_id": track_id,
            },
        )
        assert r.status_code == 201
        entry_id = r.json()["id"]

        # Queue should show the entry (participant detail will be null)
        queue_resp = await client.get(f"/api/v1/sessions/{session_id}/queue")
        assert queue_resp.status_code == 200
        current = queue_resp.json()["current"]
        assert current is not None
        assert current["id"] == entry_id

    # ------------------------------------------------------------------
    # Double-finish (calling finish twice on same entry)
    # ------------------------------------------------------------------

    async def test_double_finish_second_call_still_returns_200(self, client, app_db):
        """Calling finish twice on the same entry does not crash — the entry
        was already marked done so the second call also returns 200."""
        repo = SQLiteRepository(app_db)
        track_id = await _create_track(repo)
        session_id = await _create_session(client, "double-finish-room")
        participant_id = await _add_participant(client, session_id, "Аня")

        entry = await _add_to_queue(client, session_id, participant_id, track_id)
        await client.post(f"/api/v1/queue/{entry['id']}/start")

        # First finish
        r1 = await client.post(f"/api/v1/queue/{entry['id']}/finish")
        assert r1.status_code == 200

        # Second finish — entry still exists in DB but is already 'done'
        r2 = await client.post(f"/api/v1/queue/{entry['id']}/finish")
        # Should still return 200 (not crash), may or may not increment again
        assert r2.status_code == 200

    async def test_double_finish_does_not_corrupt_queue(self, client, app_db):
        """After a double-finish, the queue state is consistent."""
        repo = SQLiteRepository(app_db)
        track_id = await _create_track(repo)
        session_id = await _create_session(client, "double-finish-queue-room")
        participant_id = await _add_participant(client, session_id, "Борис")

        entry1 = await _add_to_queue(client, session_id, participant_id, track_id)
        entry2 = await _add_to_queue(client, session_id, participant_id, track_id)

        await client.post(f"/api/v1/queue/{entry1['id']}/start")
        await client.post(f"/api/v1/queue/{entry1['id']}/finish")
        await client.post(f"/api/v1/queue/{entry1['id']}/finish")  # double-finish

        # Queue should still show entry2 as current
        queue_resp = await client.get(f"/api/v1/sessions/{session_id}/queue")
        queue_data = queue_resp.json()
        assert queue_data["current"] is not None
        assert queue_data["current"]["id"] == entry2["id"]

    # ------------------------------------------------------------------
    # Double-start (calling start twice)
    # ------------------------------------------------------------------

    async def test_double_start_second_call_returns_200(self, client, app_db):
        """Calling start twice on the same entry returns 200 both times."""
        repo = SQLiteRepository(app_db)
        track_id = await _create_track(repo)
        session_id = await _create_session(client, "double-start-room")
        participant_id = await _add_participant(client, session_id, "Вера")

        entry = await _add_to_queue(client, session_id, participant_id, track_id)

        r1 = await client.post(f"/api/v1/queue/{entry['id']}/start")
        assert r1.status_code == 200

        r2 = await client.post(f"/api/v1/queue/{entry['id']}/start")
        assert r2.status_code == 200
        # Both calls return the same entry and clip URL
        assert r1.json()["entry_id"] == r2.json()["entry_id"]
        assert r1.json()["clip_url"] == r2.json()["clip_url"]

    # ------------------------------------------------------------------
    # Non-existent session / participant / track IDs
    # ------------------------------------------------------------------

    async def test_get_nonexistent_session_returns_404(self, client):
        """GET for a completely made-up session ID returns 404."""
        r = await client.get("/api/v1/sessions/session-that-does-not-exist")
        assert r.status_code == 404

    async def test_add_participant_to_nonexistent_session_returns_404(self, client):
        """Adding a participant to a non-existent session returns 404."""
        r = await client.post(
            "/api/v1/sessions/ghost-session/participants",
            json={"name": "Ghost"},
        )
        assert r.status_code == 404

    async def test_add_to_queue_nonexistent_session_returns_404(self, client, app_db):
        """Adding to a non-existent session returns 404."""
        repo = SQLiteRepository(app_db)
        track_id = await _create_track(repo)

        r = await client.post(
            "/api/v1/queue",
            json={
                "session_id": "no-such-session",
                "participant_id": "no-such-participant",
                "track_id": track_id,
            },
        )
        assert r.status_code == 404

    async def test_start_nonexistent_queue_entry_returns_404(self, client):
        """Starting a non-existent queue entry returns 404."""
        r = await client.post("/api/v1/queue/00000000-dead-beef-0000-000000000000/start")
        assert r.status_code == 404

    async def test_finish_nonexistent_queue_entry_returns_404(self, client):
        """Finishing a non-existent queue entry returns 404."""
        r = await client.post("/api/v1/queue/00000000-dead-beef-0000-000000000001/finish")
        assert r.status_code == 404

    async def test_skip_nonexistent_queue_entry_returns_404(self, client):
        """Skipping a non-existent queue entry returns 404."""
        r = await client.post("/api/v1/queue/00000000-dead-beef-0000-000000000002/skip")
        assert r.status_code == 404

    async def test_delete_nonexistent_queue_entry_returns_404(self, client):
        """Deleting a non-existent queue entry returns 404."""
        r = await client.delete("/api/v1/queue/00000000-dead-beef-0000-000000000003")
        assert r.status_code == 404

    async def test_get_nonexistent_track_returns_404(self, client):
        """GET for a non-existent track returns 404."""
        r = await client.get("/api/v1/tracks/track-that-does-not-exist")
        assert r.status_code == 404

    async def test_terminate_nonexistent_session_returns_404(self, client):
        """Terminating a non-existent session returns 404."""
        r = await client.delete(
            "/api/v1/sessions/ghost-session-for-delete",
            headers={"X-Admin-Secret": "test-secret"},
        )
        assert r.status_code == 404

    async def test_add_to_queue_after_session_terminated_returns_409(
        self, client, app_db
    ):
        """Attempting to add to a terminated session queue returns 409 Conflict."""
        repo = SQLiteRepository(app_db)
        track_id = await _create_track(repo)
        session_id = await _create_session(client, "terminated-queue-room")
        participant_id = await _add_participant(client, session_id, "Павел")

        # Terminate the session
        await client.delete(
            f"/api/v1/sessions/{session_id}",
            headers={"X-Admin-Secret": "test-secret"},
        )

        # Now try to add to queue
        r = await client.post(
            "/api/v1/queue",
            json={
                "session_id": session_id,
                "participant_id": participant_id,
                "track_id": track_id,
            },
        )
        assert r.status_code == 409

    async def test_add_participant_after_session_terminated_returns_409(
        self, client, app_db
    ):
        """Attempting to join a terminated session returns 409 Conflict."""
        session_id = await _create_session(client, "terminated-join-room")

        await client.delete(
            f"/api/v1/sessions/{session_id}",
            headers={"X-Admin-Secret": "test-secret"},
        )

        r = await client.post(
            f"/api/v1/sessions/{session_id}/participants",
            json={"name": "Late Joiner"},
        )
        assert r.status_code == 409

    async def test_admin_terminate_without_secret_returns_403(self, client):
        """DELETE session without X-Admin-Secret header returns 403."""
        session_id = await _create_session(client, "no-secret-room")

        r = await client.delete(f"/api/v1/sessions/{session_id}")
        assert r.status_code == 403

    async def test_admin_terminate_wrong_secret_returns_403(self, client):
        """DELETE session with wrong X-Admin-Secret returns 403."""
        session_id = await _create_session(client, "wrong-secret-room")

        r = await client.delete(
            f"/api/v1/sessions/{session_id}",
            headers={"X-Admin-Secret": "incorrect-secret"},
        )
        assert r.status_code == 403

    # ------------------------------------------------------------------
    # Search edge cases
    # ------------------------------------------------------------------

    async def test_search_empty_query_returns_zero_results(self, client):
        """GET /api/v1/tracks/search?q= returns total=0 and empty items."""
        r = await client.get("/api/v1/tracks/search?q=")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 0
        assert data["items"] == []

    async def test_search_nonexistent_artist_returns_zero_results(
        self, client, app_db
    ):
        """Searching for a term that matches nothing returns total=0."""
        repo = SQLiteRepository(app_db)
        await _create_track(repo, artist="Кино", title="Видели ночь")

        r = await client.get("/api/v1/tracks/search?q=ZZZompletelyNonexistent")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 0

    async def test_search_returns_only_ready_tracks(self, client, app_db):
        """Search results never include tracks with status != ready."""
        repo = SQLiteRepository(app_db)
        # Create a pending track with a distinctive name
        await repo.create_track(
            TrackCreate(
                artist="PendingArtistXYZ",
                title="PendingTrack",
                source="catalog",
                status="pending",
            )
        )

        r = await client.get("/api/v1/tracks/search?q=PendingArtistXYZ")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 0


# ---------------------------------------------------------------------------
# 3. Stress test — 5 participants, 20 tracks in queue
# ---------------------------------------------------------------------------


class TestStressQueueOrdering:
    """Verify queue ordering and rotation correctness under load."""

    async def test_five_participants_twenty_tracks_queue_ordering(
        self, client, app_db
    ):
        """
        Setup: 5 participants, 4 tracks each (20 total) added in round-robin.
        Assertion: queue returns entries in insertion order; each participant's
        entries are spread across the queue correctly.
        """
        repo = SQLiteRepository(app_db)
        session_id = await _create_session(client, "stress-test-room")

        # Create 5 participants
        participant_ids = []
        for i in range(5):
            pid = await _add_participant(client, session_id, f"Singer{i+1}")
            participant_ids.append(pid)

        # Create 4 tracks for each participant (20 unique tracks)
        track_ids = []
        for i in range(20):
            tid = await _create_track(
                repo,
                artist=f"Band{i}",
                title=f"Song{i}",
                clip_path=f"/media/track{i}.mp4",
                duration_sec=180 + i,
            )
            track_ids.append(tid)

        # Add tracks in round-robin order: p0,p1,p2,p3,p4,p0,p1,...
        entries = []
        for i in range(20):
            participant_id = participant_ids[i % 5]
            track_id = track_ids[i]
            entry = await _add_to_queue(client, session_id, participant_id, track_id)
            entries.append(entry)

        # Verify all 20 entries are in the queue
        queue_resp = await client.get(f"/api/v1/sessions/{session_id}/queue")
        assert queue_resp.status_code == 200
        queue_data = queue_resp.json()

        assert queue_data["current"] is not None
        all_queue_entries = [queue_data["current"]] + queue_data["upcoming"]
        assert len(all_queue_entries) == 20

        # Verify entries are in ascending order_position
        positions = [e["order_position"] for e in all_queue_entries]
        assert positions == sorted(positions), "Queue entries must be in position order"

        # Verify first entry is the first one we added
        assert all_queue_entries[0]["id"] == entries[0]["id"]

        # Verify last entry is the last one we added
        assert all_queue_entries[-1]["id"] == entries[-1]["id"]

        # Verify round-robin participant distribution in first 5 entries
        first_five_participants = [
            e["participant"]["id"] for e in all_queue_entries[:5]
        ]
        assert set(first_five_participants) == set(participant_ids), (
            "First 5 entries should cover all 5 participants"
        )

    async def test_sequential_play_and_finish_twenty_entries(self, client, app_db):
        """
        Play through all 20 entries sequentially: start then finish each one.
        After all entries are done, the queue must be empty.
        """
        repo = SQLiteRepository(app_db)
        session_id = await _create_session(client, "sequential-play-room")

        participant_ids = []
        for i in range(5):
            pid = await _add_participant(client, session_id, f"Player{i+1}")
            participant_ids.append(pid)

        track_ids = []
        for i in range(20):
            tid = await _create_track(
                repo, artist=f"Artist{i}", title=f"Track{i}"
            )
            track_ids.append(tid)

        entries = []
        for i in range(20):
            entry = await _add_to_queue(
                client, session_id, participant_ids[i % 5], track_ids[i]
            )
            entries.append(entry)

        # Play through all entries
        for entry in entries:
            start_r = await client.post(f"/api/v1/queue/{entry['id']}/start")
            assert start_r.status_code == 200

            finish_r = await client.post(f"/api/v1/queue/{entry['id']}/finish")
            assert finish_r.status_code == 200

        # After all entries are done, queue should be empty
        final_queue = await client.get(f"/api/v1/sessions/{session_id}/queue")
        assert final_queue.status_code == 200
        final_data = final_queue.json()
        assert final_data["current"] is None
        assert final_data["upcoming"] == []

    async def test_play_counts_correct_after_sequential_finish(self, client, app_db):
        """
        After all 20 entries are played, each track's play_count must equal 1
        and each participant's tracks_played must equal 4.
        """
        repo = SQLiteRepository(app_db)
        session_id = await _create_session(client, "play-count-stress-room")

        participant_ids = []
        for i in range(5):
            pid = await _add_participant(client, session_id, f"Counter{i+1}")
            participant_ids.append(pid)

        track_ids = []
        for i in range(20):
            tid = await _create_track(
                repo, artist=f"BandCount{i}", title=f"SongCount{i}"
            )
            track_ids.append(tid)

        entries = []
        for i in range(20):
            entry = await _add_to_queue(
                client, session_id, participant_ids[i % 5], track_ids[i]
            )
            entries.append(entry)

        for entry in entries:
            await client.post(f"/api/v1/queue/{entry['id']}/start")
            await client.post(f"/api/v1/queue/{entry['id']}/finish")

        # Each track played exactly once
        for tid in track_ids:
            track = await repo.get_track(tid)
            assert track is not None
            assert track.play_count == 1, (
                f"Track {tid} expected play_count=1, got {track.play_count}"
            )

        # Each participant sang exactly 4 tracks (20 tracks / 5 participants)
        for pid in participant_ids:
            participant = await repo.get_participant(pid)
            assert participant is not None
            assert participant.tracks_played == 4, (
                f"Participant {pid} expected tracks_played=4, "
                f"got {participant.tracks_played}"
            )

    async def test_skip_rotation_requeues_at_end(self, client, app_db):
        """
        Skipping entries correctly appends them at the end of the queue.
        After skipping entry1, it re-appears at the last position.
        """
        repo = SQLiteRepository(app_db)
        session_id = await _create_session(client, "skip-rotation-room")

        p1 = await _add_participant(client, session_id, "Skip1")
        p2 = await _add_participant(client, session_id, "Skip2")
        p3 = await _add_participant(client, session_id, "Skip3")

        tid = await _create_track(repo, artist="Skippable", title="Track")

        e1 = await _add_to_queue(client, session_id, p1, tid)
        e2 = await _add_to_queue(client, session_id, p2, tid)
        e3 = await _add_to_queue(client, session_id, p3, tid)

        # Skip entry1 — it should be re-queued after e3
        skip_r = await client.post(f"/api/v1/queue/{e1['id']}/skip")
        assert skip_r.status_code == 200
        new_entry = skip_r.json()

        # New entry must be at a position after e3
        assert new_entry["order_position"] > e3["order_position"]

        # Queue should now be: e2 (current), e3, new_entry
        queue_resp = await client.get(f"/api/v1/sessions/{session_id}/queue")
        queue_data = queue_resp.json()
        all_active = [queue_data["current"]] + queue_data["upcoming"]
        assert len(all_active) == 3

        active_ids = [e["id"] for e in all_active]
        assert e1["id"] not in active_ids  # original skip entry gone
        assert e2["id"] in active_ids
        assert e3["id"] in active_ids
        assert new_entry["id"] in active_ids

        # Current should be e2 (first remaining queued entry)
        assert queue_data["current"]["id"] == e2["id"]
        # Last should be the newly re-queued entry
        assert queue_data["upcoming"][-1]["id"] == new_entry["id"]

    async def test_large_queue_current_is_always_lowest_position(
        self, client, app_db
    ):
        """
        With 20 entries, the 'current' entry reported by GET /queue must always
        be the one with the lowest order_position among active entries.
        """
        repo = SQLiteRepository(app_db)
        session_id = await _create_session(client, "lowest-position-room")

        participant_id = await _add_participant(client, session_id, "Solo Singer")
        track_id = await _create_track(repo)

        entries = []
        for _ in range(20):
            e = await _add_to_queue(client, session_id, participant_id, track_id)
            entries.append(e)

        # Play and finish 10 entries; after each, verify current has lowest position
        for i in range(10):
            queue_resp = await client.get(f"/api/v1/sessions/{session_id}/queue")
            queue_data = queue_resp.json()
            current = queue_data["current"]
            upcoming = queue_data["upcoming"]

            assert current is not None, f"Expected current entry at step {i}"
            current_pos = current["order_position"]
            for u in upcoming:
                assert u["order_position"] > current_pos, (
                    f"Step {i}: upcoming entry has lower position than current"
                )

            await client.post(f"/api/v1/queue/{current['id']}/start")
            await client.post(f"/api/v1/queue/{current['id']}/finish")
