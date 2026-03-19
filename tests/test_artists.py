"""Tests for artist image infrastructure (Phase R5)."""

from __future__ import annotations

import pytest

from karaoke_shared.models.artist import Artist
from karaoke_shared.models.track import TrackCreate
from karaoke_shared.repositories.sqlite_repository import SQLiteRepository


class TestArtistModel:

    def test_construction(self):
        now = "2024-01-01T00:00:00+00:00"
        artist = Artist(name="Queen", image_path="abc123.jpg", source="spotify", created_at=now, updated_at=now)
        assert artist.name == "Queen"
        assert artist.image_path == "abc123.jpg"

    def test_no_image(self):
        now = "2024-01-01T00:00:00+00:00"
        artist = Artist(name="Unknown", created_at=now, updated_at=now)
        assert artist.image_path is None


class TestSQLiteArtists:

    async def test_upsert_artist(self, sqlite_repo: SQLiteRepository):
        await sqlite_repo.upsert_artist("Queen", "abc123.jpg", "spotify")
        artist = await sqlite_repo.get_artist("Queen")
        assert artist is not None
        assert artist["name"] == "Queen"
        assert artist["image_path"] == "abc123.jpg"

    async def test_upsert_updates_existing(self, sqlite_repo: SQLiteRepository):
        await sqlite_repo.upsert_artist("Queen", None, "placeholder")
        await sqlite_repo.upsert_artist("Queen", "abc123.jpg", "spotify")
        artist = await sqlite_repo.get_artist("Queen")
        assert artist is not None
        assert artist["image_path"] == "abc123.jpg"
        assert artist["source"] == "spotify"

    async def test_get_artist_not_found(self, sqlite_repo: SQLiteRepository):
        assert await sqlite_repo.get_artist("Nobody") is None

    async def test_get_artists_without_images(self, sqlite_repo: SQLiteRepository):
        await sqlite_repo.create_track(
            TrackCreate(artist="Queen", title="BR", source="catalog", status="ready")
        )
        await sqlite_repo.create_track(
            TrackCreate(artist="Кино", title="ГК", source="catalog", status="ready")
        )
        # Queen has image, Кино doesn't
        await sqlite_repo.upsert_artist("Queen", "abc.jpg", "spotify")

        missing = await sqlite_repo.get_artists_without_images()
        assert "Кино" in missing
        assert "Queen" not in missing
