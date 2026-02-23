"""Integration tests for the Sessions API endpoints.

Tests cover:
    POST   /api/v1/sessions
    GET    /api/v1/sessions/{session_id}
    POST   /api/v1/sessions/{session_id}/participants
    DELETE /api/v1/sessions/{session_id}

Setup: each test gets a fresh in-memory SQLite database via the ``client``
fixture defined in conftest.py.  No QDrant is needed for session/queue tests.

Run with:
    PYTHONPATH=backend ADMIN_SECRET=test-secret \\
        python -m pytest tests/test_api_sessions.py -v
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# POST /api/v1/sessions
# ---------------------------------------------------------------------------


class TestCreateSession:
    async def test_returns_201_with_session_fields(self, client):
        """Creating a session with a room_id returns 201 and correct body."""
        response = await client.post("/api/v1/sessions", json={"room_id": "room-A"})

        assert response.status_code == 201
        body = response.json()
        assert body["room_id"] == "room-A"
        assert body["status"] == "active"
        assert "id" in body
        assert body["id"] != ""
        assert "created_at" in body
        assert body["terminated_at"] is None

    async def test_each_session_gets_unique_id(self, client):
        """Two sessions for the same room get different IDs."""
        r1 = await client.post("/api/v1/sessions", json={"room_id": "room-B"})
        r2 = await client.post("/api/v1/sessions", json={"room_id": "room-B"})

        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["id"] != r2.json()["id"]

    async def test_missing_room_id_returns_422(self, client):
        """Omitting room_id yields a 422 Unprocessable Entity."""
        response = await client.post("/api/v1/sessions", json={})

        assert response.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/v1/sessions/{session_id}
# ---------------------------------------------------------------------------


class TestGetSession:
    async def test_returns_session_with_empty_participants(self, client):
        """A newly created session has an empty participant list."""
        create_resp = await client.post(
            "/api/v1/sessions", json={"room_id": "room-C"}
        )
        session_id = create_resp.json()["id"]

        response = await client.get(f"/api/v1/sessions/{session_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == session_id
        assert body["room_id"] == "room-C"
        assert body["status"] == "active"
        assert body["participants"] == []

    async def test_returns_session_with_participants_after_join(self, client):
        """GET returns the full participant list after participants have joined."""
        create_resp = await client.post(
            "/api/v1/sessions", json={"room_id": "room-D"}
        )
        session_id = create_resp.json()["id"]

        await client.post(
            f"/api/v1/sessions/{session_id}/participants", json={"name": "Алиса"}
        )
        await client.post(
            f"/api/v1/sessions/{session_id}/participants", json={"name": "Борис"}
        )

        response = await client.get(f"/api/v1/sessions/{session_id}")

        assert response.status_code == 200
        participants = response.json()["participants"]
        assert len(participants) == 2
        names = {p["display_name"] for p in participants}
        assert names == {"Алиса", "Борис"}

    async def test_nonexistent_session_returns_404(self, client):
        """GET for an unknown session ID returns 404."""
        response = await client.get("/api/v1/sessions/does-not-exist")

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/sessions/{session_id}/participants
# ---------------------------------------------------------------------------


class TestAddParticipant:
    async def test_with_explicit_name_returns_201_and_display_name(self, client):
        """Adding a participant with a name stores and returns that name."""
        create_resp = await client.post(
            "/api/v1/sessions", json={"room_id": "room-E"}
        )
        session_id = create_resp.json()["id"]

        response = await client.post(
            f"/api/v1/sessions/{session_id}/participants", json={"name": "Вася"}
        )

        assert response.status_code == 201
        body = response.json()
        assert body["display_name"] == "Вася"
        assert body["session_id"] == session_id
        assert "id" in body
        assert body["id"] != ""

    async def test_without_name_auto_generates_non_empty_nickname(self, client):
        """Omitting the name field triggers auto-nickname generation."""
        create_resp = await client.post(
            "/api/v1/sessions", json={"room_id": "room-F"}
        )
        session_id = create_resp.json()["id"]

        # Send an empty body (name=None is the default, which triggers auto-nick)
        response = await client.post(
            f"/api/v1/sessions/{session_id}/participants", json={}
        )

        assert response.status_code == 201
        display_name = response.json()["display_name"]
        assert isinstance(display_name, str)
        assert len(display_name) > 0

    async def test_auto_nicknames_are_unique_within_session(self, client):
        """Auto-generated nicknames must not repeat within the same session."""
        create_resp = await client.post(
            "/api/v1/sessions", json={"room_id": "room-G"}
        )
        session_id = create_resp.json()["id"]

        names = []
        for _ in range(5):
            r = await client.post(
                f"/api/v1/sessions/{session_id}/participants", json={}
            )
            assert r.status_code == 201
            names.append(r.json()["display_name"])

        # All names must be distinct
        assert len(set(names)) == 5

    async def test_nonexistent_session_returns_404(self, client):
        """Adding a participant to a missing session returns 404."""
        response = await client.post(
            "/api/v1/sessions/no-such-session/participants", json={"name": "Ivan"}
        )

        assert response.status_code == 404

    async def test_terminated_session_returns_409(self, client):
        """Adding a participant to a terminated session returns 409 Conflict."""
        create_resp = await client.post(
            "/api/v1/sessions", json={"room_id": "room-H"}
        )
        session_id = create_resp.json()["id"]

        # Terminate the session first
        await client.delete(
            f"/api/v1/sessions/{session_id}",
            headers={"X-Admin-Secret": "test-secret"},
        )

        response = await client.post(
            f"/api/v1/sessions/{session_id}/participants", json={"name": "Lena"}
        )

        assert response.status_code == 409

    async def test_no_body_returns_422(self, client):
        """POSTing to participants with no body at all returns 422."""
        create_resp = await client.post(
            "/api/v1/sessions", json={"room_id": "room-I"}
        )
        session_id = create_resp.json()["id"]

        # Send request with no body (content-type not set, body missing)
        response = await client.post(
            f"/api/v1/sessions/{session_id}/participants"
        )

        assert response.status_code == 422


# ---------------------------------------------------------------------------
# DELETE /api/v1/sessions/{session_id}
# ---------------------------------------------------------------------------


class TestTerminateSession:
    async def test_correct_secret_returns_204(self, client):
        """DELETE with the correct X-Admin-Secret header returns 204."""
        create_resp = await client.post(
            "/api/v1/sessions", json={"room_id": "room-J"}
        )
        session_id = create_resp.json()["id"]

        response = await client.delete(
            f"/api/v1/sessions/{session_id}",
            headers={"X-Admin-Secret": "test-secret"},
        )

        assert response.status_code == 204

    async def test_terminated_session_status_is_updated(self, client):
        """After DELETE, GET on the session shows status=terminated."""
        create_resp = await client.post(
            "/api/v1/sessions", json={"room_id": "room-K"}
        )
        session_id = create_resp.json()["id"]

        await client.delete(
            f"/api/v1/sessions/{session_id}",
            headers={"X-Admin-Secret": "test-secret"},
        )

        get_resp = await client.get(f"/api/v1/sessions/{session_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["status"] == "terminated"
        assert get_resp.json()["terminated_at"] is not None

    async def test_wrong_secret_returns_403(self, client):
        """DELETE with an incorrect admin secret returns 403 Forbidden."""
        create_resp = await client.post(
            "/api/v1/sessions", json={"room_id": "room-L"}
        )
        session_id = create_resp.json()["id"]

        response = await client.delete(
            f"/api/v1/sessions/{session_id}",
            headers={"X-Admin-Secret": "wrong-secret"},
        )

        assert response.status_code == 403

    async def test_missing_secret_header_returns_403(self, client):
        """DELETE without X-Admin-Secret header at all returns 403."""
        create_resp = await client.post(
            "/api/v1/sessions", json={"room_id": "room-M"}
        )
        session_id = create_resp.json()["id"]

        response = await client.delete(f"/api/v1/sessions/{session_id}")

        assert response.status_code == 403

    async def test_nonexistent_session_returns_404(self, client):
        """DELETE for an unknown session ID returns 404 (auth checked first)."""
        response = await client.delete(
            "/api/v1/sessions/ghost-session",
            headers={"X-Admin-Secret": "test-secret"},
        )

        assert response.status_code == 404
