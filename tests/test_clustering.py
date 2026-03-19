"""Tests for catalog clustering (Phase R2).

Coverage:
- CatalogCluster model
- SQLite repository CRUD for clusters
- Track catalog_cluster_id field
"""

from __future__ import annotations

import json
import uuid

import pytest

from karaoke_shared.models.catalog_cluster import CatalogCluster
from karaoke_shared.models.track import Track, TrackCreate
from karaoke_shared.repositories.sqlite_repository import SQLiteRepository


def _uid() -> str:
    return str(uuid.uuid4())


# ===========================================================================
# TestCatalogClusterModel
# ===========================================================================


class TestCatalogClusterModel:

    def test_construction(self):
        now = "2024-01-01T00:00:00+00:00"
        cluster = CatalogCluster(
            id=1,
            centroid_audio=[0.1] * 45,
            centroid_lyrics=[0.2] * 384,
            track_count=100,
            created_at=now,
            updated_at=now,
        )
        assert cluster.id == 1
        assert len(cluster.centroid_audio) == 45
        assert len(cluster.centroid_lyrics) == 384
        assert cluster.track_count == 100


# ===========================================================================
# TestSQLiteClusterCRUD
# ===========================================================================


class TestSQLiteClusterCRUD:

    async def test_create_cluster(self, sqlite_repo: SQLiteRepository):
        audio = [0.1] * 45
        lyrics = [0.2] * 384
        cluster_id = await sqlite_repo.create_catalog_cluster(audio, lyrics, 50)
        assert cluster_id is not None
        assert isinstance(cluster_id, int)

    async def test_get_all_clusters(self, sqlite_repo: SQLiteRepository):
        await sqlite_repo.create_catalog_cluster([0.1] * 45, [0.2] * 384, 50)
        await sqlite_repo.create_catalog_cluster([0.3] * 45, [0.4] * 384, 30)

        clusters = await sqlite_repo.get_all_clusters()
        assert len(clusters) == 2
        assert clusters[0].track_count == 50
        assert clusters[1].track_count == 30
        assert len(clusters[0].centroid_audio) == 45
        assert len(clusters[0].centroid_lyrics) == 384

    async def test_clear_clusters(self, sqlite_repo: SQLiteRepository):
        cid = await sqlite_repo.create_catalog_cluster([0.1] * 45, [0.2] * 384, 10)

        track = await sqlite_repo.create_track(
            TrackCreate(artist="Test", title="Song", source="catalog", status="ready")
        )
        await sqlite_repo.assign_cluster(track.id, cid)

        # Verify assignment
        t = await sqlite_repo.get_track(track.id)
        assert t is not None
        assert t.catalog_cluster_id == cid

        # Clear
        await sqlite_repo.clear_clusters()

        clusters = await sqlite_repo.get_all_clusters()
        assert len(clusters) == 0

        t2 = await sqlite_repo.get_track(track.id)
        assert t2 is not None
        assert t2.catalog_cluster_id is None

    async def test_assign_cluster(self, sqlite_repo: SQLiteRepository):
        cid = await sqlite_repo.create_catalog_cluster([0.1] * 45, [0.2] * 384, 5)
        track = await sqlite_repo.create_track(
            TrackCreate(artist="Test", title="Song", source="catalog", status="ready")
        )

        await sqlite_repo.assign_cluster(track.id, cid)

        updated = await sqlite_repo.get_track(track.id)
        assert updated is not None
        assert updated.catalog_cluster_id == cid


# ===========================================================================
# TestTrackClusterField
# ===========================================================================


class TestTrackClusterField:

    def test_track_default_cluster_is_none(self):
        now = "2024-01-01T00:00:00+00:00"
        track = Track(
            id=_uid(), artist="T", title="S", source="catalog",
            created_at=now, updated_at=now,
        )
        assert track.catalog_cluster_id is None

    def test_track_with_cluster(self):
        now = "2024-01-01T00:00:00+00:00"
        track = Track(
            id=_uid(), artist="T", title="S", source="catalog",
            catalog_cluster_id=3, created_at=now, updated_at=now,
        )
        assert track.catalog_cluster_id == 3

    async def test_create_track_with_cluster(self, sqlite_repo: SQLiteRepository):
        cid = await sqlite_repo.create_catalog_cluster([0.1] * 45, [0.2] * 384, 5)
        track = await sqlite_repo.create_track(
            TrackCreate(
                artist="Test", title="Song", source="catalog",
                status="ready", catalog_cluster_id=cid,
            )
        )
        assert track.catalog_cluster_id == cid
