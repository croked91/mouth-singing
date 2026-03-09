"""Unit tests for LyricsSearcher (primary Genius + web-search fallback)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.pipeline.lyrics_searcher import (
    LyricsAPIError,
    LyricsNotFoundError,
    LyricsResult,
    LyricsSearcher,
)


def _make_openai_response(content: str) -> dict:
    """Build a minimal OpenAI chat completion response."""
    return {
        "choices": [
            {"message": {"content": content}}
        ]
    }


def _identify_json(found=True, **overrides) -> str:
    """Build a valid identification JSON string."""
    data = {
        "found": found,
        "artist": "Test Artist",
        "title": "Test Song",
        "confidence": "high",
    }
    if not found:
        data["not_found_reason"] = "could not identify"
    data.update(overrides)
    return json.dumps(data)


def _genius_search_response(url: str = "https://genius.com/test-lyrics") -> dict:
    """Build a minimal Genius API search response."""
    return {
        "response": {
            "hits": [
                {
                    "result": {
                        "url": url,
                        "full_title": "Test Song by Test Artist",
                        "id": 12345,
                    }
                }
            ]
        }
    }


def _genius_lyrics_html(lyrics_text: str = "line one\nline two\n\nline three") -> str:
    """Build a minimal Genius page HTML with lyrics containers."""
    escaped = lyrics_text.replace("\n", "<br/>")
    return f"""<html><body>
    <div data-lyrics-container="true">{escaped}</div>
    </body></html>"""


def _web_search_response(
    found: bool = True,
    artist: str = "WS Artist",
    title: str = "WS Song",
    urls: list[str] | None = None,
) -> dict:
    """Build a minimal OpenAI Responses API output with web search."""
    if urls is None:
        urls = ["https://example.com/lyrics"]

    content_text = json.dumps({
        "found": found,
        "artist": artist,
        "title": title,
        "lyrics_urls": urls,
        **({"reason": "not found"} if not found else {}),
    })

    return {
        "output": [
            {"type": "web_search_call", "id": "ws_test"},
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": content_text}
                ],
            },
        ]
    }


def _generic_lyrics_html(text: str = "Fallback lyrics text that is definitely long enough to pass the minimum check") -> str:
    """Build a minimal page with lyrics in a class containing 'lyric'."""
    escaped = text.replace("\n", "<br/>")
    return f'<html><body><div class="lyrics-body">{escaped}</div></body></html>'


@pytest.fixture
def searcher():
    return LyricsSearcher(
        openai_api_key="sk-test-key",
        genius_token="genius-test-token",
        model="gpt-4o-mini",
        timeout=5.0,
        max_retries=1,
        openai_base_url="https://api.openai.com",
    )


# ======================================================================
# Primary path success
# ======================================================================


class TestPrimaryPathSuccess:
    """Test successful primary path (LLM identify → Genius)."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_full_pipeline_success(self, searcher):
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json=_make_openai_response(_identify_json()),
            )
        )
        respx.get("https://api.genius.com/search").mock(
            return_value=httpx.Response(200, json=_genius_search_response())
        )
        respx.get("https://genius.com/test-lyrics").mock(
            return_value=httpx.Response(200, text=_genius_lyrics_html())
        )

        result = await searcher.search(
            asr_text="some transcribed text",
            detected_language="en",
            artist_hint="Test",
            title_hint="Song",
        )

        assert isinstance(result, LyricsResult)
        assert result.artist == "Test Artist"
        assert result.title == "Test Song"
        assert "line one" in result.lyrics
        assert result.source_note == "genius.com"

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_with_no_hints(self, searcher):
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json=_make_openai_response(_identify_json()),
            )
        )
        respx.get("https://api.genius.com/search").mock(
            return_value=httpx.Response(200, json=_genius_search_response())
        )
        respx.get("https://genius.com/test-lyrics").mock(
            return_value=httpx.Response(200, text=_genius_lyrics_html())
        )

        result = await searcher.search(asr_text="some text", detected_language="ru")
        assert result.artist == "Test Artist"


# ======================================================================
# Fallback path
# ======================================================================


class TestFallbackPath:
    """Test web-search fallback when primary path fails."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_fallback_on_genius_no_results(self, searcher):
        """Genius has no hits → fallback to web search → scrape URL."""
        # Primary: LLM identifies, Genius empty
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json=_make_openai_response(_identify_json()),
            )
        )
        genius_route = respx.get("https://api.genius.com/search").mock(
            return_value=httpx.Response(200, json={"response": {"hits": []}})
        )
        # Fallback: web search returns URLs
        respx.post("https://api.openai.com/v1/responses").mock(
            return_value=httpx.Response(
                200,
                json=_web_search_response(
                    urls=["https://example.com/lyrics-page"]
                ),
            )
        )
        # Genius retry after web search (different artist/title) also fails
        # (genius_route is already mocked to return empty)
        # Scrape fallback URL
        respx.get("https://example.com/lyrics-page").mock(
            return_value=httpx.Response(
                200, text=_generic_lyrics_html("These are the real lyrics from the fallback site and they are complete"),
            )
        )

        result = await searcher.search(asr_text="text", detected_language="en")
        assert result.artist == "WS Artist"
        assert result.source_note == "web_search+example.com"
        assert "real lyrics" in result.lyrics

    @respx.mock
    @pytest.mark.asyncio
    async def test_fallback_on_llm_not_found(self, searcher):
        """LLM says found=false → fallback to web search."""
        # Primary: LLM not found
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json=_make_openai_response(_identify_json(found=False)),
            )
        )
        # Fallback: web search finds it
        respx.post("https://api.openai.com/v1/responses").mock(
            return_value=httpx.Response(
                200,
                json=_web_search_response(
                    artist="Found Artist", title="Found Song",
                    urls=["https://genius.com/found-song-lyrics"],
                ),
            )
        )
        # Genius retry with new artist/title works
        respx.get("https://api.genius.com/search").mock(
            return_value=httpx.Response(
                200,
                json=_genius_search_response("https://genius.com/found-song-lyrics"),
            )
        )
        respx.get("https://genius.com/found-song-lyrics").mock(
            return_value=httpx.Response(
                200,
                text=_genius_lyrics_html("Found via web search fallback and genius scrape"),
            )
        )

        result = await searcher.search(asr_text="noise", detected_language="ru")
        assert result.artist == "Found Artist"
        assert result.source_note == "web_search+genius"

    @respx.mock
    @pytest.mark.asyncio
    async def test_fallback_also_fails(self, searcher):
        """Both primary and fallback fail → LyricsNotFoundError."""
        # Primary: LLM not found
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json=_make_openai_response(_identify_json(found=False)),
            )
        )
        # Fallback: web search also not found
        respx.post("https://api.openai.com/v1/responses").mock(
            return_value=httpx.Response(
                200,
                json=_web_search_response(found=False),
            )
        )

        with pytest.raises(LyricsNotFoundError, match="Primary.*Fallback"):
            await searcher.search(asr_text="noise", detected_language="en")


# ======================================================================
# API errors (should NOT trigger fallback)
# ======================================================================


class TestAPIErrors:
    """Test error handling with retries and fallback attempts."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_openai_server_error_retries_then_fallback(self, searcher):
        """500 on identify → retries → fallback web search also fails."""
        route = respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        # Fallback also hits OpenAI (responses API) — also 500
        respx.post("https://api.openai.com/v1/responses").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )

        with pytest.raises(LyricsNotFoundError, match="Primary.*Server error"):
            await searcher.search(asr_text="text", detected_language="en")

        assert route.call_count == 2  # 1 initial + 1 retry

    @respx.mock
    @pytest.mark.asyncio
    async def test_openai_rate_limit_retries_then_fallback(self, searcher):
        route = respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(429, text="Rate limited")
        )
        respx.post("https://api.openai.com/v1/responses").mock(
            return_value=httpx.Response(429, text="Rate limited")
        )

        with pytest.raises(LyricsNotFoundError, match="Primary.*Rate limited"):
            await searcher.search(asr_text="text", detected_language="en")

        assert route.call_count == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_openai_auth_error_fallback_also_fails(self, searcher):
        """401 on identify → fallback web search also fails (same key)."""
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )
        respx.post("https://api.openai.com/v1/responses").mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )

        with pytest.raises(LyricsNotFoundError, match="Primary.*API error 401"):
            await searcher.search(asr_text="text", detected_language="en")

    @respx.mock
    @pytest.mark.asyncio
    async def test_invalid_json_triggers_fallback(self, searcher):
        """Invalid JSON from identify → fallback web search also fails."""
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json=_make_openai_response("I can't help with that"),
            )
        )
        respx.post("https://api.openai.com/v1/responses").mock(
            return_value=httpx.Response(
                200, json=_web_search_response(found=False),
            )
        )

        with pytest.raises(LyricsNotFoundError, match="Primary.*Invalid JSON"):
            await searcher.search(asr_text="text", detected_language="en")


# ======================================================================
# Genius-specific edge cases (primary path)
# ======================================================================


class TestGeniusEdgeCases:
    """Test Genius scraping edge cases."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_genius_search_500_triggers_fallback(self, searcher):
        """Genius API 500 triggers web search fallback."""
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json=_make_openai_response(_identify_json()),
            )
        )
        respx.get("https://api.genius.com/search").mock(
            return_value=httpx.Response(500, text="error")
        )
        # Fallback: web search finds lyrics URL
        respx.post("https://api.openai.com/v1/responses").mock(
            return_value=httpx.Response(
                200,
                json=_web_search_response(urls=["https://example.com/lyrics"]),
            )
        )
        respx.get("https://example.com/lyrics").mock(
            return_value=httpx.Response(
                200, text=_generic_lyrics_html("Lyrics recovered via web search fallback after genius failure"),
            )
        )

        result = await searcher.search(asr_text="text", detected_language="en")
        assert "web_search" in result.source_note

    @respx.mock
    @pytest.mark.asyncio
    async def test_section_markers_removed(self, searcher):
        html = _genius_lyrics_html(
            "[Verse 1]\nHello world\n\n[Chorus]\nLa la la"
        )
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json=_make_openai_response(_identify_json()),
            )
        )
        respx.get("https://api.genius.com/search").mock(
            return_value=httpx.Response(200, json=_genius_search_response())
        )
        respx.get("https://genius.com/test-lyrics").mock(
            return_value=httpx.Response(200, text=html)
        )

        result = await searcher.search(asr_text="text", detected_language="en")
        assert "[Verse 1]" not in result.lyrics
        assert "Hello world" in result.lyrics

    @respx.mock
    @pytest.mark.asyncio
    async def test_genius_header_noise_removed(self, searcher):
        raw_lyrics = (
            "24 ContributorsTranslationsSong Title Lyrics\n"
            "[Verse 1]\nActual lyrics here, this is the real song text"
        )
        html = _genius_lyrics_html(raw_lyrics)
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json=_make_openai_response(_identify_json()),
            )
        )
        respx.get("https://api.genius.com/search").mock(
            return_value=httpx.Response(200, json=_genius_search_response())
        )
        respx.get("https://genius.com/test-lyrics").mock(
            return_value=httpx.Response(200, text=html)
        )

        result = await searcher.search(asr_text="text", detected_language="en")
        assert "Contributors" not in result.lyrics
        assert "Actual lyrics here, this is the real song text" in result.lyrics

    @respx.mock
    @pytest.mark.asyncio
    async def test_invalid_json_with_extractable_object(self, searcher):
        content = f"Here is the identification:\n{_identify_json()}"
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json=_make_openai_response(content),
            )
        )
        respx.get("https://api.genius.com/search").mock(
            return_value=httpx.Response(200, json=_genius_search_response())
        )
        respx.get("https://genius.com/test-lyrics").mock(
            return_value=httpx.Response(200, text=_genius_lyrics_html())
        )

        result = await searcher.search(asr_text="text", detected_language="en")
        assert result.artist == "Test Artist"


class TestLLMExtraction:
    """Test LLM-based lyrics extraction from generic pages."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_llm_extraction_when_no_css_match(self, searcher):
        """Page has no lyric CSS markers → falls through to LLM extraction."""
        # Primary fails
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            side_effect=[
                # 1st call: identification (primary)
                httpx.Response(
                    200, json=_make_openai_response(_identify_json(found=False)),
                ),
                # 3rd call: LLM extraction from page
                httpx.Response(
                    200,
                    json=_make_openai_response(
                        "First line of lyrics\nSecond line of lyrics\n\nThird line of lyrics"
                    ),
                ),
            ]
        )
        # Fallback: web search
        respx.post("https://api.openai.com/v1/responses").mock(
            return_value=httpx.Response(
                200,
                json=_web_search_response(urls=["https://obscure-site.com/song"]),
            )
        )
        # Genius fails for web search artist/title
        respx.get("https://api.genius.com/search").mock(
            return_value=httpx.Response(200, json={"response": {"hits": []}})
        )
        # Page has no lyrics CSS markers — plain divs with enough text
        page_body = (
            "<div>Navigation Home About</div>"
            "<div>First line of lyrics\nSecond line of lyrics\n\n"
            "Third line of lyrics\nFourth line of lyrics</div>"
            "<div>Share this page</div>"
        )
        respx.get("https://obscure-site.com/song").mock(
            return_value=httpx.Response(
                200,
                text=f"<html><body>{page_body}</body></html>",
            )
        )

        result = await searcher.search(asr_text="text", detected_language="en")
        assert "First line" in result.lyrics
        assert result.source_note == "web_search+obscure-site.com"
