"""Integration tests for the tracks and playback API endpoints.

Tests cover:
- GET /api/v1/tracks/popular
- GET /api/v1/tracks/search
- GET /api/v1/tracks/search/suggest
- GET /api/v1/tracks/{track_id}
- POST /api/v1/tracks/upload
- GET /api/v1/tracks/{track_id}/stream
"""

from __future__ import annotations

import io

import pytest

from karaoke_shared.models.track import TrackCreate
from karaoke_shared.repositories.sqlite_repository import SQLiteRepository


@pytest.fixture
def tmp_media_root(monkeypatch, tmp_path):
    """Override settings.media_root to a writable temp directory."""
    import app.config as config_module
    import app.api.v1.tracks as tracks_module

    monkeypatch.setattr(config_module.settings, "media_root", str(tmp_path))
    # The tracks router reads settings at call time, so the monkeypatch is
    # sufficient — no need to reload the module.
    return tmp_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_ready_track(
    repo: SQLiteRepository,
    artist: str = "The Beatles",
    title: str = "Come Together",
    mp3_path: str | None = None,
    clip_path: str | None = None,
) -> str:
    """Insert a ready track and return its ID."""
    track = await repo.create_track(
        TrackCreate(
            artist=artist,
            title=title,
            source="catalog",
            status="ready",
            mp3_path=mp3_path,
            clip_path=clip_path,
        )
    )
    return track.id


# ---------------------------------------------------------------------------
# Popular tracks
# ---------------------------------------------------------------------------


class TestListPopular:
    async def test_returns_empty_list_when_no_ready_tracks(self, client):
        response = await client.get("/api/v1/tracks/popular")
        assert response.status_code == 200
        assert response.json() == []

    async def test_returns_ready_tracks_only(self, client, app_db):
        repo = SQLiteRepository(app_db)
        await _create_ready_track(repo, artist="Queen", title="Bohemian Rhapsody")
        # Also create a pending track that should NOT appear.
        await repo.create_track(
            TrackCreate(artist="Artist", title="Pending", source="catalog", status="pending")
        )

        response = await client.get("/api/v1/tracks/popular")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["artist"] == "Queen"

    async def test_limit_param_is_respected(self, client, app_db):
        repo = SQLiteRepository(app_db)
        for i in range(5):
            await _create_ready_track(repo, artist=f"Artist {i}", title=f"Song {i}")

        response = await client.get("/api/v1/tracks/popular?limit=2")
        assert response.status_code == 200
        assert len(response.json()) == 2


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestSearchTracks:
    async def test_empty_query_returns_empty_result(self, client):
        response = await client.get("/api/v1/tracks/search?q=")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["items"] == []

    async def test_fts_returns_matching_tracks(self, client, app_db):
        repo = SQLiteRepository(app_db)
        await _create_ready_track(repo, artist="Queen", title="Bohemian Rhapsody")
        await _create_ready_track(repo, artist="David Bowie", title="Heroes")

        response = await client.get("/api/v1/tracks/search?q=Queen")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] >= 1
        artists = [item["artist"] for item in data["items"]]
        assert "Queen" in artists

    async def test_search_result_has_expected_fields(self, client, app_db):
        repo = SQLiteRepository(app_db)
        await _create_ready_track(repo, artist="Nirvana", title="Smells Like Teen Spirit")

        response = await client.get("/api/v1/tracks/search?q=Nirvana")
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) >= 1
        item = items[0]
        assert "id" in item
        assert "artist" in item
        assert "title" in item
        assert "clip_ready" in item
        assert item["clip_ready"] is True  # status == "ready"

    async def test_invalid_fts_syntax_returns_empty_not_error(self, client, app_db):
        # Unbalanced quote is malformed FTS syntax — should return empty, not 500.
        response = await client.get('/api/v1/tracks/search?q="')
        assert response.status_code == 200
        assert response.json()["total"] == 0


# ---------------------------------------------------------------------------
# Suggest
# ---------------------------------------------------------------------------


class TestSuggest:
    async def test_empty_query_returns_empty_list(self, client):
        response = await client.get("/api/v1/tracks/search/suggest?q=")
        assert response.status_code == 200
        assert response.json() == []

    async def test_suggests_matching_artist_prefix(self, client, app_db):
        repo = SQLiteRepository(app_db)
        await _create_ready_track(repo, artist="Radiohead", title="Creep")
        await _create_ready_track(repo, artist="Red Hot Chili Peppers", title="Scar Tissue")

        response = await client.get("/api/v1/tracks/search/suggest?q=Red")
        assert response.status_code == 200
        suggestions = response.json()
        assert len(suggestions) >= 1
        assert any("Red Hot Chili Peppers" in s for s in suggestions)

    async def test_only_suggests_ready_tracks(self, client, app_db):
        repo = SQLiteRepository(app_db)
        await repo.create_track(
            TrackCreate(
                artist="Pending Artist",
                title="Pending Song",
                source="catalog",
                status="pending",
            )
        )

        response = await client.get("/api/v1/tracks/search/suggest?q=Pending")
        assert response.status_code == 200
        assert response.json() == []

    async def test_suggestion_format_is_artist_dash_title(self, client, app_db):
        repo = SQLiteRepository(app_db)
        await _create_ready_track(repo, artist="Pink Floyd", title="Wish You Were Here")

        response = await client.get("/api/v1/tracks/search/suggest?q=Pink")
        assert response.status_code == 200
        suggestions = response.json()
        assert "Pink Floyd — Wish You Were Here" in suggestions


# ---------------------------------------------------------------------------
# Get track by ID
# ---------------------------------------------------------------------------


class TestGetTrack:
    async def test_returns_404_for_unknown_id(self, client):
        response = await client.get("/api/v1/tracks/nonexistent-id")
        assert response.status_code == 404

    async def test_returns_track_when_found(self, client, app_db):
        repo = SQLiteRepository(app_db)
        track_id = await _create_ready_track(repo, artist="Metallica", title="Enter Sandman")

        response = await client.get(f"/api/v1/tracks/{track_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == track_id
        assert data["artist"] == "Metallica"
        assert data["title"] == "Enter Sandman"


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


class TestUploadTrack:
    def _mp3_bytes(self) -> bytes:
        """Return minimal fake MP3 bytes (ID3 header) for testing."""
        return b"ID3" + b"\x00" * 10

    async def test_upload_creates_track_and_job(self, client, tmp_media_root):
        file_content = self._mp3_bytes()
        response = await client.post(
            "/api/v1/tracks/upload",
            files={"file": ("song.mp3", io.BytesIO(file_content), "audio/mpeg")},
            data={"artist": "Test Artist", "title": "Test Song"},
        )
        assert response.status_code == 202
        data = response.json()
        assert "track_id" in data
        assert "job_id" in data
        assert data["status"] == "pending"

    async def test_upload_uses_default_names_when_omitted(self, client, tmp_media_root):
        file_content = self._mp3_bytes()
        response = await client.post(
            "/api/v1/tracks/upload",
            files={"file": ("song.mp3", io.BytesIO(file_content), "audio/mpeg")},
        )
        assert response.status_code == 202
        track_id = response.json()["track_id"]

        # Verify the track was created with the default names.
        track_response = await client.get(f"/api/v1/tracks/{track_id}")
        assert track_response.status_code == 200
        data = track_response.json()
        assert data["artist"] == "Unknown Artist"
        assert data["title"] == "Unknown Track"

    async def test_upload_rejects_non_mp3_extension(self, client, tmp_media_root):
        response = await client.post(
            "/api/v1/tracks/upload",
            files={"file": ("song.ogg", io.BytesIO(b"OGG"), "audio/ogg")},
        )
        assert response.status_code == 422

    async def test_upload_rejects_wrong_content_type(self, client, tmp_media_root):
        response = await client.post(
            "/api/v1/tracks/upload",
            files={"file": ("song.mp3", io.BytesIO(b"data"), "video/mp4")},
        )
        assert response.status_code == 422

    async def test_upload_rejects_file_over_50mb(self, client, tmp_media_root):
        # 51 MB of zeros — never hits disk because we check size first.
        big_content = b"\x00" * (51 * 1024 * 1024)
        response = await client.post(
            "/api/v1/tracks/upload",
            files={"file": ("big.mp3", io.BytesIO(big_content), "audio/mpeg")},
        )
        assert response.status_code == 413


# ---------------------------------------------------------------------------
# Stream
# ---------------------------------------------------------------------------


class TestStreamTrack:
    async def test_returns_404_for_unknown_track(self, client):
        response = await client.get("/api/v1/tracks/no-such-track/stream")
        assert response.status_code == 404

    async def test_returns_404_when_track_has_no_file(self, client, app_db, tmp_media_root):
        repo = SQLiteRepository(app_db)
        track_id = await _create_ready_track(repo)  # no mp3_path or clip_path

        response = await client.get(f"/api/v1/tracks/{track_id}/stream")
        assert response.status_code == 404

    async def test_returns_404_when_file_missing_from_disk(self, client, app_db, tmp_media_root):
        repo = SQLiteRepository(app_db)
        missing = str(tmp_media_root / "missing.mp3")
        track_id = await _create_ready_track(repo, mp3_path=missing)

        response = await client.get(f"/api/v1/tracks/{track_id}/stream")
        assert response.status_code == 404

    async def test_returns_200_for_full_file_request(self, client, app_db, tmp_media_root):
        mp3_file = tmp_media_root / "test.mp3"
        mp3_file.write_bytes(b"FAKEMP3DATA" * 100)

        repo = SQLiteRepository(app_db)
        track_id = await _create_ready_track(repo, mp3_path=str(mp3_file))

        response = await client.get(f"/api/v1/tracks/{track_id}/stream")
        assert response.status_code == 200
        assert "audio/mpeg" in response.headers.get("content-type", "")

    async def test_returns_206_for_range_request(self, client, app_db, tmp_media_root):
        mp3_file = tmp_media_root / "range.mp3"
        mp3_file.write_bytes(b"A" * 1000)

        repo = SQLiteRepository(app_db)
        track_id = await _create_ready_track(repo, mp3_path=str(mp3_file))

        response = await client.get(
            f"/api/v1/tracks/{track_id}/stream",
            headers={"Range": "bytes=0-99"},
        )
        assert response.status_code == 206
        assert response.headers.get("content-range", "").startswith("bytes 0-99/")
        assert len(response.content) == 100

