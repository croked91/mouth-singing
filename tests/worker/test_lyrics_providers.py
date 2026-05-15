"""Tests for the three live lyrics providers (genius / lrclib / lyricsovh).

Uses ``respx`` to intercept httpx requests so no real network is touched.
Each provider is checked for:
  * happy path → returns LyricsCandidate(s) with parsed lyrics
  * 4xx / 5xx → graceful return (empty list / None) without exception
  * network error → graceful return
  * empty / too-short lyrics → filtered out
"""

from __future__ import annotations

import httpx
import respx

from worker.common.lyrics.base_provider import LyricsCandidate
from worker.common.lyrics.providers.genius import GeniusProvider
from worker.common.lyrics.providers.lrclib import LRCLibProvider
from worker.common.lyrics.providers.lyricsovh import LyricsOvhProvider


# ---------------------------------------------------------------------------
# LRCLib
# ---------------------------------------------------------------------------

class TestLRCLib:
    @respx.mock
    async def test_returns_first_candidate_from_combined_query(self):
        respx.get("https://lrclib.net/api/search").mock(
            return_value=httpx.Response(200, json=[
                {
                    "trackName": "Bohemian Rhapsody",
                    "artistName": "Queen",
                    "plainLyrics": "Is this the real life? Is this just fantasy?",
                },
                {
                    "trackName": "Other",
                    "artistName": "Other",
                    "plainLyrics": "x" * 30,
                },
            ])
        )

        provider = LRCLibProvider(timeout=1.0)
        result = await provider.search_by_metadata("Queen", "Bohemian Rhapsody")

        assert isinstance(result, LyricsCandidate)
        assert result.artist == "Queen"
        assert result.title == "Bohemian Rhapsody"
        assert result.source == "lrclib"
        assert "real life" in result.lyrics

    @respx.mock
    async def test_falls_back_to_structured_search_when_combined_empty(self):
        # First call (combined) returns []; second call (structured) returns a hit
        route = respx.get("https://lrclib.net/api/search").mock(
            side_effect=[
                httpx.Response(200, json=[]),
                httpx.Response(200, json=[
                    {"trackName": "T", "artistName": "A",
                     "plainLyrics": "long enough lyrics body to pass the 20-char filter"},
                ]),
            ]
        )

        provider = LRCLibProvider(timeout=1.0)
        result = await provider.search_by_metadata("A", "T")

        assert result is not None
        assert route.call_count == 2
        # Inspect the second call's params — must be structured, not 'q='
        second_request = route.calls[1].request
        assert "track_name=T" in str(second_request.url)
        assert "artist_name=A" in str(second_request.url)

    @respx.mock
    async def test_returns_none_on_500(self):
        respx.get("https://lrclib.net/api/search").mock(
            return_value=httpx.Response(500),
        )
        provider = LRCLibProvider(timeout=1.0)
        result = await provider.search_by_metadata("X", "Y")
        assert result is None

    @respx.mock
    async def test_filters_out_too_short_lyrics(self):
        respx.get("https://lrclib.net/api/search").mock(
            return_value=httpx.Response(200, json=[
                {"trackName": "T", "artistName": "A", "plainLyrics": "short"},
            ])
        )
        provider = LRCLibProvider(timeout=1.0)
        # The first short item is dropped, then the structured-search fallback
        # also returns nothing, so the final result is None.
        respx.get("https://lrclib.net/api/search").mock(
            return_value=httpx.Response(200, json=[]),
        )
        result = await provider.search_by_metadata("A", "T")
        assert result is None

    @respx.mock
    async def test_network_error_returns_none(self):
        respx.get("https://lrclib.net/api/search").mock(
            side_effect=httpx.ConnectError("boom"),
        )
        provider = LRCLibProvider(timeout=1.0)
        result = await provider.search_by_metadata("A", "T")
        assert result is None


# ---------------------------------------------------------------------------
# Lyrics.ovh
# ---------------------------------------------------------------------------

class TestLyricsOvh:
    @respx.mock
    async def test_returns_candidate_on_200(self):
        respx.get("https://api.lyrics.ovh/v1/Queen/Bohemian Rhapsody").mock(
            return_value=httpx.Response(200, json={
                "lyrics": "Is this the real life? Is this just fantasy?\n",
            }),
        )

        provider = LyricsOvhProvider(timeout=1.0)
        result = await provider.search_by_metadata("Queen", "Bohemian Rhapsody")

        assert isinstance(result, LyricsCandidate)
        assert result.source == "lyricsovh"
        assert result.lyrics.startswith("Is this the real life")
        # whitespace must be trimmed
        assert not result.lyrics.endswith("\n")

    @respx.mock
    async def test_returns_none_on_404(self):
        respx.get("https://api.lyrics.ovh/v1/A/T").mock(
            return_value=httpx.Response(404, json={"error": "not found"}),
        )
        provider = LyricsOvhProvider(timeout=1.0)
        assert await provider.search_by_metadata("A", "T") is None

    @respx.mock
    async def test_returns_none_when_lyrics_too_short(self):
        respx.get("https://api.lyrics.ovh/v1/A/T").mock(
            return_value=httpx.Response(200, json={"lyrics": "short"}),
        )
        provider = LyricsOvhProvider(timeout=1.0)
        assert await provider.search_by_metadata("A", "T") is None

    @respx.mock
    async def test_timeout_returns_none(self):
        respx.get("https://api.lyrics.ovh/v1/A/T").mock(
            side_effect=httpx.TimeoutException("slow"),
        )
        provider = LyricsOvhProvider(timeout=1.0)
        assert await provider.search_by_metadata("A", "T") is None


# ---------------------------------------------------------------------------
# Genius
# ---------------------------------------------------------------------------

_GENIUS_LYRICS_HTML = """
<html><body>
<div data-lyrics-container="true">
1 ContributorsBohemian Rhapsody Lyrics
Is this the real life?<br/>Is this just fantasy?<br/>
Caught in a landslide, no escape from reality.
</div>
</body></html>
"""


class TestGenius:
    @respx.mock
    async def test_search_returns_candidates_with_scraped_lyrics(self):
        respx.get("https://api.genius.com/search").mock(
            return_value=httpx.Response(200, json={
                "response": {
                    "hits": [
                        {"result": {
                            "url": "https://genius.com/queen-bohemian-rhapsody-lyrics",
                            "title": "Bohemian Rhapsody",
                            "primary_artist": {"name": "Queen"},
                        }},
                    ],
                },
            }),
        )
        respx.get("https://genius.com/queen-bohemian-rhapsody-lyrics").mock(
            return_value=httpx.Response(200, text=_GENIUS_LYRICS_HTML),
        )

        provider = GeniusProvider(token="fake-token", timeout=1.0)
        results = await provider.search_by_text("real life fantasy")

        assert len(results) == 1
        assert results[0].artist == "Queen"
        assert results[0].title == "Bohemian Rhapsody"
        assert "Is this the real life?" in results[0].lyrics
        assert "Caught in a landslide" in results[0].lyrics
        assert results[0].source == "genius"

    @respx.mock
    async def test_search_failure_returns_empty(self):
        respx.get("https://api.genius.com/search").mock(
            return_value=httpx.Response(401),
        )
        provider = GeniusProvider(token="bad-token", timeout=1.0)
        assert await provider.search_by_text("anything") == []

    @respx.mock
    async def test_no_hits_returns_empty(self):
        respx.get("https://api.genius.com/search").mock(
            return_value=httpx.Response(200, json={"response": {"hits": []}}),
        )
        provider = GeniusProvider(token="fake-token", timeout=1.0)
        assert await provider.search_by_text("garbage query") == []

    @respx.mock
    async def test_skips_hits_with_no_url(self):
        respx.get("https://api.genius.com/search").mock(
            return_value=httpx.Response(200, json={
                "response": {"hits": [{"result": {"url": None}}]},
            }),
        )
        provider = GeniusProvider(token="fake-token", timeout=1.0)
        assert await provider.search_by_text("anything") == []

    @respx.mock
    async def test_scrape_failure_skips_hit(self):
        respx.get("https://api.genius.com/search").mock(
            return_value=httpx.Response(200, json={
                "response": {"hits": [
                    {"result": {
                        "url": "https://genius.com/foo",
                        "title": "T",
                        "primary_artist": {"name": "A"},
                    }},
                ]},
            }),
        )
        respx.get("https://genius.com/foo").mock(
            return_value=httpx.Response(503),
        )
        provider = GeniusProvider(token="fake-token", timeout=1.0)
        assert await provider.search_by_text("anything") == []
