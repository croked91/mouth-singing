"""Tests for popularity scoring (Phase R1).

Coverage:
- PopularityCategory enum values
- Track model with popularity fields
- SQLite repository update_popularity method
- Categorization logic from parse_karaoke_charts
"""

from __future__ import annotations

import uuid

import pytest

from karaoke_shared.constants import PopularityCategory
from karaoke_shared.models.track import Track, TrackCreate
from karaoke_shared.repositories.sqlite_repository import SQLiteRepository


def _uid() -> str:
    return str(uuid.uuid4())


# ===========================================================================
# TestPopularityCategory
# ===========================================================================


class TestPopularityCategory:
    """Verify PopularityCategory enum values."""

    def test_enum_values(self):
        assert PopularityCategory.ETERNAL_HIT == "eternal_hit"
        assert PopularityCategory.CURRENT_HIT == "current_hit"
        assert PopularityCategory.FORMER_HIT == "former_hit"
        assert PopularityCategory.ARTIST_BEST == "artist_best"
        assert PopularityCategory.REGULAR == "regular"

    def test_all_members(self):
        values = {m.value for m in PopularityCategory}
        assert values == {"eternal_hit", "current_hit", "former_hit", "artist_best", "regular"}


# ===========================================================================
# TestTrackModel
# ===========================================================================


class TestTrackPopularityFields:
    """Track model includes popularity fields with correct defaults."""

    def test_track_defaults(self):
        now = "2024-01-01T00:00:00+00:00"
        track = Track(
            id=_uid(),
            artist="Test",
            title="Song",
            source="catalog",
            created_at=now,
            updated_at=now,
        )
        assert track.popularity_category == "regular"
        assert track.chart_count == 0
        assert track.chart_last_seen is None

    def test_track_create_defaults(self):
        tc = TrackCreate(artist="Test", title="Song", source="catalog")
        assert tc.popularity_category == "regular"
        assert tc.chart_count == 0
        assert tc.chart_last_seen is None

    def test_track_with_custom_popularity(self):
        now = "2024-01-01T00:00:00+00:00"
        track = Track(
            id=_uid(),
            artist="Queen",
            title="Bohemian Rhapsody",
            source="catalog",
            popularity_category="eternal_hit",
            chart_count=8,
            chart_last_seen="2026-03-19",
            created_at=now,
            updated_at=now,
        )
        assert track.popularity_category == "eternal_hit"
        assert track.chart_count == 8


# ===========================================================================
# TestSQLitePopularity
# ===========================================================================


class TestSQLitePopularity:
    """Integration tests for popularity fields in SQLite."""

    async def test_create_track_with_popularity(self, sqlite_repo: SQLiteRepository):
        track = await sqlite_repo.create_track(
            TrackCreate(
                artist="Queen",
                title="Bohemian Rhapsody",
                source="catalog",
                status="ready",
                popularity_category="eternal_hit",
                chart_count=5,
            )
        )
        assert track.popularity_category == "eternal_hit"
        assert track.chart_count == 5

    async def test_create_track_default_popularity(self, sqlite_repo: SQLiteRepository):
        track = await sqlite_repo.create_track(
            TrackCreate(artist="Unknown", title="Song", source="catalog", status="ready")
        )
        assert track.popularity_category == "regular"
        assert track.chart_count == 0

    async def test_update_popularity(self, sqlite_repo: SQLiteRepository):
        track = await sqlite_repo.create_track(
            TrackCreate(artist="Test", title="Song", source="catalog", status="ready")
        )
        assert track.popularity_category == "regular"

        await sqlite_repo.update_popularity(
            track.id, "current_hit", chart_count=3, chart_last_seen="2026-03-19"
        )

        updated = await sqlite_repo.get_track(track.id)
        assert updated is not None
        assert updated.popularity_category == "current_hit"
        assert updated.chart_count == 3
        assert updated.chart_last_seen == "2026-03-19"

    async def test_popularity_preserved_on_play_count_increment(self, sqlite_repo: SQLiteRepository):
        track = await sqlite_repo.create_track(
            TrackCreate(
                artist="Queen",
                title="BR",
                source="catalog",
                status="ready",
                popularity_category="eternal_hit",
                chart_count=8,
            )
        )
        await sqlite_repo.increment_play_count(track.id)

        updated = await sqlite_repo.get_track(track.id)
        assert updated is not None
        assert updated.popularity_category == "eternal_hit"
        assert updated.chart_count == 8
        assert updated.play_count == 1


# ===========================================================================
# TestCategorizationLogic
# ===========================================================================


class TestCategorizationLogic:
    """Unit tests for the categorization algorithm."""

    def test_eternal_hit_matched(self):
        from scripts.parse_karaoke_charts import CatalogTrack, categorize_tracks

        catalog = [CatalogTrack(id="1", artist="Кино", title="Группа крови")]
        eternal = [("Кино", "Группа крови")]
        results = categorize_tracks(catalog, [], eternal)
        assert results["1"][0] == "eternal_hit"

    def test_current_hit_by_top10(self):
        from scripts.parse_karaoke_charts import CatalogTrack, ChartEntry, categorize_tracks

        catalog = [CatalogTrack(id="1", artist="NewArtist", title="HitSong")]
        entries = [ChartEntry(artist="NewArtist", title="HitSong", source="chart1", position=5)]
        results = categorize_tracks(catalog, entries, [])
        assert results["1"][0] == "current_hit"

    def test_current_hit_by_3_charts(self):
        from scripts.parse_karaoke_charts import CatalogTrack, ChartEntry, categorize_tracks

        catalog = [CatalogTrack(id="1", artist="Artist", title="Song")]
        entries = [
            ChartEntry(artist="Artist", title="Song", source="chart1"),
            ChartEntry(artist="Artist", title="Song", source="chart2"),
            ChartEntry(artist="Artist", title="Song", source="chart3"),
        ]
        results = categorize_tracks(catalog, entries, [])
        assert results["1"][0] == "current_hit"

    def test_regular_when_no_matches(self):
        from scripts.parse_karaoke_charts import CatalogTrack, categorize_tracks

        catalog = [CatalogTrack(id="1", artist="Nobody", title="Unknown Song")]
        results = categorize_tracks(catalog, [], [])
        assert results["1"][0] == "regular"

    def test_artist_best(self):
        from scripts.parse_karaoke_charts import CatalogTrack, ChartEntry, categorize_tracks

        catalog = [
            CatalogTrack(id="1", artist="Band", title="Hit"),
            CatalogTrack(id="2", artist="Band", title="Deep Cut"),
        ]
        entries = [ChartEntry(artist="Band", title="Hit", source="chart1")]
        results = categorize_tracks(catalog, entries, [])
        # "Hit" is in 1 chart (not enough for current_hit) but is artist's best
        assert results["1"][0] == "artist_best"
        assert results["2"][0] == "regular"

    def test_fuzzy_match_works(self):
        from scripts.parse_karaoke_charts import fuzzy_match

        assert fuzzy_match("Кино", "Группа Крови", "кино", "группа крови")
        assert fuzzy_match("Queen", "Bohemian Rhapsody", "queen", "bohemian rhapsody")
        assert not fuzzy_match("Queen", "Bohemian Rhapsody", "Metallica", "Nothing Else Matters")

    def test_eternal_has_priority_over_current(self):
        from scripts.parse_karaoke_charts import CatalogTrack, ChartEntry, categorize_tracks

        catalog = [CatalogTrack(id="1", artist="Кино", title="Группа крови")]
        entries = [
            ChartEntry(artist="Кино", title="Группа крови", source="chart1", position=1),
            ChartEntry(artist="Кино", title="Группа крови", source="chart2"),
            ChartEntry(artist="Кино", title="Группа крови", source="chart3"),
        ]
        eternal = [("Кино", "Группа крови")]
        results = categorize_tracks(catalog, entries, eternal)
        # eternal_hit has priority even though it qualifies as current_hit too
        assert results["1"][0] == "eternal_hit"
