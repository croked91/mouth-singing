"""Tests for mood tags (Phase R3).

Coverage:
- MoodTag model
- SQLite repository CRUD
- GET /tags endpoint (filtering by covered clusters)
"""

from __future__ import annotations

import uuid

import pytest

from karaoke_shared.models.mood_tag import MoodTag, MoodTagResponse
from karaoke_shared.models.track import TrackCreate
from karaoke_shared.repositories.sqlite_repository import SQLiteRepository


def _uid() -> str:
    return str(uuid.uuid4())


# ===========================================================================
# TestMoodTagModel
# ===========================================================================


class TestMoodTagModel:

    def test_construction(self):
        tag = MoodTag(id=1, name="Рок", cluster_id=3, created_at="2024-01-01")
        assert tag.id == 1
        assert tag.name == "Рок"
        assert tag.cluster_id == 3

    def test_response_model(self):
        resp = MoodTagResponse(id=1, name="Рок")
        assert resp.id == 1
        assert resp.name == "Рок"


# ===========================================================================
# TestSQLiteMoodTags
# ===========================================================================


class TestSQLiteMoodTags:

    async def test_create_tag(self, sqlite_repo: SQLiteRepository):
        cid = await sqlite_repo.create_catalog_cluster([0.1] * 45, [0.2] * 384, 10)
        tag_id = await sqlite_repo.create_mood_tag("Рок", cid)
        assert tag_id is not None
        assert isinstance(tag_id, int)

    async def test_get_all_tags(self, sqlite_repo: SQLiteRepository):
        cid = await sqlite_repo.create_catalog_cluster([0.1] * 45, [0.2] * 384, 10)
        await sqlite_repo.create_mood_tag("Рок", cid)
        await sqlite_repo.create_mood_tag("Погнали", cid)

        tags = await sqlite_repo.get_all_tags()
        assert len(tags) == 2
        assert tags[0]["name"] == "Рок"
        assert tags[1]["name"] == "Погнали"

    async def test_get_tag(self, sqlite_repo: SQLiteRepository):
        cid = await sqlite_repo.create_catalog_cluster([0.1] * 45, [0.2] * 384, 10)
        tag_id = await sqlite_repo.create_mood_tag("Рок", cid)

        tag = await sqlite_repo.get_tag(tag_id)
        assert tag is not None
        assert tag["name"] == "Рок"
        assert tag["cluster_id"] == cid

    async def test_get_tag_not_found(self, sqlite_repo: SQLiteRepository):
        tag = await sqlite_repo.get_tag(999)
        assert tag is None

    async def test_get_tags_excluding_clusters(self, sqlite_repo: SQLiteRepository):
        c1 = await sqlite_repo.create_catalog_cluster([0.1] * 45, [0.2] * 384, 10)
        c2 = await sqlite_repo.create_catalog_cluster([0.3] * 45, [0.4] * 384, 20)

        await sqlite_repo.create_mood_tag("Рок", c1)
        await sqlite_repo.create_mood_tag("Попс", c2)

        # Exclude cluster 1 → only get tags from cluster 2
        tags = await sqlite_repo.get_tags_excluding_clusters({c1})
        assert len(tags) == 1
        assert tags[0]["name"] == "Попс"

    async def test_get_tags_excluding_none(self, sqlite_repo: SQLiteRepository):
        c1 = await sqlite_repo.create_catalog_cluster([0.1] * 45, [0.2] * 384, 10)
        await sqlite_repo.create_mood_tag("Рок", c1)
        await sqlite_repo.create_mood_tag("Душевное", c1)

        # Exclude nothing → get all
        tags = await sqlite_repo.get_tags_excluding_clusters(set())
        assert len(tags) == 2

    async def test_clear_mood_tags(self, sqlite_repo: SQLiteRepository):
        c1 = await sqlite_repo.create_catalog_cluster([0.1] * 45, [0.2] * 384, 10)
        await sqlite_repo.create_mood_tag("Рок", c1)

        await sqlite_repo.clear_mood_tags()

        tags = await sqlite_repo.get_all_tags()
        assert len(tags) == 0


# ===========================================================================
# TestTagsEndpoint
# ===========================================================================


class TestTagsEndpoint:
    """Integration tests for GET /tags."""

    async def test_returns_200(self, client, app_db):
        from karaoke_shared.repositories import SQLiteRepository
        from app.main import app

        repo = SQLiteRepository(app_db)

        # Create session
        r = await client.post("/api/v1/sessions", json={"room_id": "room-tag-1"})
        session_id = r.json()["id"]

        r = await client.get(
            "/api/v1/tags",
            params={"session_id": session_id},
        )
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    async def test_returns_tags_as_list(self, client, app_db):
        from karaoke_shared.repositories import SQLiteRepository

        repo = SQLiteRepository(app_db)

        # Create a cluster and tag
        cid = await repo.create_catalog_cluster([0.1] * 45, [0.2] * 384, 10)
        await repo.create_mood_tag("Рок", cid)

        r = await client.post("/api/v1/sessions", json={"room_id": "room-tag-2"})
        session_id = r.json()["id"]

        r = await client.get(
            "/api/v1/tags",
            params={"session_id": session_id},
        )
        assert r.status_code == 200
        tags = r.json()
        assert len(tags) >= 1
        assert tags[0]["name"] == "Рок"
        assert "id" in tags[0]
