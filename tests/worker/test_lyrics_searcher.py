"""Unit tests for LyricsAgent (DeepSeek + Yandex Search agent)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from worker.common.lyrics_agent import LyricsAgent
from worker.common.lyrics_searcher import (
    LyricsAPIError,
    LyricsNotFoundError,
    LyricsResult,
    clean_lyrics,
)


def _make_agent_fixture() -> LyricsAgent:
    return LyricsAgent(
        deepseek_api_key="sk-test-deepseek",
        yandex_search_api_key="test-yandex-key",
        yandex_search_folder_id="test-folder-id",
        model="deepseek-chat",
        max_iterations=3,
        timeout=5.0,
    )


@pytest.fixture
def agent():
    return _make_agent_fixture()


def _mock_chat_response(content: str, tool_calls=None):
    """Build a mock OpenAI chat completion response."""
    message = MagicMock()
    message.content = content
    message.tool_calls = tool_calls
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    return response


def _mock_tool_call(name: str, arguments: dict, call_id: str = "call_1"):
    """Build a mock tool call object."""
    tc = MagicMock()
    tc.function.name = name
    tc.function.arguments = json.dumps(arguments)
    tc.id = call_id
    return tc


# ======================================================================
# Successful search — JSON response
# ======================================================================


class TestAgentSearchSuccess:
    """Test successful agent search returning structured JSON."""

    async def test_json_response_with_metadata(self, agent):
        """Agent returns valid JSON with artist, title, lyrics."""
        agent_json = json.dumps({
            "artist": "Кино",
            "title": "Группа крови",
            "lyrics": "Тёплое место, но улицы ждут\nОтпечатков наших ног",
        })

        with patch.object(agent, "_run_agent", return_value=agent_json):
            result = await agent.search(
                asr_text="тёплое место но улицы ждут",
                detected_language="ru",
                artist_hint="Кино",
                title_hint="Группа крови",
            )

        assert isinstance(result, LyricsResult)
        assert result.artist == "Кино"
        assert result.title == "Группа крови"
        assert "Тёплое место" in result.lyrics
        assert result.language == "ru"
        assert result.source_note == "deepseek+yandex"

    async def test_json_embedded_in_text(self, agent):
        """Agent returns JSON wrapped in text — still parses correctly."""
        raw = 'Вот результат:\n{"artist": "Ласковый май", "title": "Белые розы", "lyrics": "Белые розы, белые розы"}'

        with patch.object(agent, "_run_agent", return_value=raw):
            result = await agent.search(
                asr_text="белые розы",
                detected_language="ru",
            )

        assert result.artist == "Ласковый май"
        assert result.title == "Белые розы"

    async def test_no_hints_provided(self, agent):
        """Search works without artist/title hints."""
        agent_json = json.dumps({
            "artist": "Artist",
            "title": "Song",
            "lyrics": "Some lyrics text that is long enough to pass checks",
        })

        with patch.object(agent, "_run_agent", return_value=agent_json):
            result = await agent.search(
                asr_text="some lyrics text",
                detected_language="en",
            )

        assert result.artist == "Artist"


# ======================================================================
# Plain text fallback — metadata extraction
# ======================================================================


class TestPlainTextFallback:
    """Test when agent returns plain lyrics without JSON metadata."""

    async def test_plain_text_triggers_metadata_extraction(self, agent):
        """Plain text response → _extract_metadata called."""
        plain_lyrics = "Белые розы, белые розы\nБеззащитны шипы\nЧто с ними сделаешь"

        with (
            patch.object(agent, "_run_agent", return_value=plain_lyrics),
            patch.object(
                agent,
                "_extract_metadata",
                return_value=("Ласковый май", "Белые розы"),
            ),
        ):
            result = await agent.search(
                asr_text="белые розы",
                detected_language="ru",
            )

        assert result.artist == "Ласковый май"
        assert result.title == "Белые розы"
        assert "Белые розы, белые розы" in result.lyrics

    async def test_metadata_extraction_fails_uses_hints(self, agent):
        """Metadata extraction fails → falls back to hints."""
        plain_lyrics = "Some lyrics that are definitely long enough for checks"

        with (
            patch.object(agent, "_run_agent", return_value=plain_lyrics),
            patch.object(
                agent,
                "_extract_metadata",
                side_effect=Exception("LLM failed"),
            ),
        ):
            result = await agent.search(
                asr_text="some text",
                detected_language="en",
                artist_hint="Hint Artist",
                title_hint="Hint Song",
            )

        assert result.artist == "Hint Artist"
        assert result.title == "Hint Song"

    async def test_no_metadata_no_hints_uses_unknown(self, agent):
        """No metadata and no hints → 'Unknown'."""
        plain_lyrics = "Some lyrics that are definitely long enough for checks"

        with (
            patch.object(agent, "_run_agent", return_value=plain_lyrics),
            patch.object(
                agent,
                "_extract_metadata",
                side_effect=Exception("LLM failed"),
            ),
        ):
            result = await agent.search(
                asr_text="some text",
                detected_language="en",
            )

        assert result.artist == "Unknown"
        assert result.title == "Unknown"


# ======================================================================
# Not found scenarios
# ======================================================================


class TestNotFound:
    """Test scenarios where lyrics cannot be found."""

    async def test_agent_returns_not_found_marker(self, agent):
        """Agent explicitly says 'текст не найден'."""
        with patch.object(agent, "_run_agent", return_value="текст не найден"):
            with pytest.raises(LyricsNotFoundError, match="could not find"):
                await agent.search(asr_text="noise", detected_language="ru")

    async def test_agent_returns_empty(self, agent):
        """Agent returns empty string."""
        with patch.object(agent, "_run_agent", return_value=""):
            with pytest.raises(LyricsNotFoundError, match="could not find"):
                await agent.search(asr_text="noise", detected_language="ru")

    async def test_agent_returns_short_text(self, agent):
        """Agent returns text shorter than 20 chars."""
        with patch.object(agent, "_run_agent", return_value='{"artist":"A","title":"B","lyrics":"short"}'):
            with pytest.raises(LyricsNotFoundError, match="very short"):
                await agent.search(asr_text="noise", detected_language="en")


# ======================================================================
# API errors
# ======================================================================


class TestAPIErrors:
    """Test API error handling."""

    async def test_deepseek_api_error_raises_lyrics_api_error(self, agent):
        """DeepSeek API error → LyricsAPIError."""
        from openai import APIError

        with patch.object(
            agent,
            "_run_agent",
            side_effect=APIError(
                message="Unauthorized",
                request=MagicMock(),
                body=None,
            ),
        ):
            with pytest.raises(LyricsAPIError, match="Lyrics agent error"):
                await agent.search(asr_text="text", detected_language="en")

    async def test_max_iterations_raises_not_found(self, agent):
        """Agent exhausts iterations → LyricsNotFoundError."""
        with patch.object(
            agent,
            "_run_agent",
            side_effect=LyricsNotFoundError("Agent exhausted max iterations"),
        ):
            with pytest.raises(LyricsNotFoundError, match="max iterations"):
                await agent.search(asr_text="text", detected_language="en")


# ======================================================================
# Agent tool-calling loop
# ======================================================================


class TestAgentLoop:
    """Test the internal agent tool-calling loop."""

    def test_agent_loop_single_iteration(self, agent):
        """Agent returns immediately without tool calls."""
        response_json = json.dumps({
            "artist": "Test", "title": "Song", "lyrics": "Full lyrics here",
        })

        mock_response = _mock_chat_response(response_json)

        with patch("worker.common.lyrics_agent.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_response

            result = agent._run_agent("some whisper text")

        assert "Full lyrics here" in result

    def test_agent_loop_with_tool_calls(self, agent):
        """Agent makes tool calls then returns final response."""
        search_tool_call = _mock_tool_call(
            "web_search", {"query": "test lyrics"}, "call_1",
        )

        # First response: tool call
        tool_response = _mock_chat_response(None, tool_calls=[search_tool_call])
        # Second response: final answer
        final_json = json.dumps({
            "artist": "Test", "title": "Song", "lyrics": "Found lyrics",
        })
        final_response = _mock_chat_response(final_json)

        with (
            patch("worker.common.lyrics_agent.OpenAI") as mock_openai_cls,
            patch("worker.common.lyrics_agent._web_search", return_value='[{"title":"t","href":"u","body":"b"}]'),
        ):
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.chat.completions.create.side_effect = [
                tool_response,
                final_response,
            ]

            result = agent._run_agent("some whisper text")

        assert "Found lyrics" in result


# ======================================================================
# Response parsing
# ======================================================================


class TestResponseParsing:
    """Test _parse_agent_response static method."""

    def test_valid_json(self):
        raw = json.dumps({"artist": "A", "title": "T", "lyrics": "L"})
        artist, title, lyrics = LyricsAgent._parse_agent_response(raw, None, None)
        assert artist == "A"
        assert title == "T"
        assert lyrics == "L"

    def test_json_in_text(self):
        raw = 'Here is the result: {"artist": "A", "title": "T", "lyrics": "L"}'
        artist, title, lyrics = LyricsAgent._parse_agent_response(raw, None, None)
        assert artist == "A"
        assert lyrics == "L"

    def test_plain_text(self):
        raw = "Just some lyrics\nwithout any JSON"
        artist, title, lyrics = LyricsAgent._parse_agent_response(raw, "hint_a", "hint_t")
        assert artist == ""
        assert title == ""
        assert lyrics == "Just some lyrics\nwithout any JSON"

    def test_json_missing_lyrics_key(self):
        raw = json.dumps({"artist": "A", "title": "T"})
        artist, title, lyrics = LyricsAgent._parse_agent_response(raw, None, None)
        assert artist == ""
        assert lyrics == raw.strip()


# ======================================================================
# clean_lyrics helper
# ======================================================================


class TestCleanLyrics:
    """Test the clean_lyrics helper function."""

    def test_removes_section_markers(self):
        raw = "[Verse 1]\nHello world\n\n[Chorus]\nLa la la"
        result = clean_lyrics(raw)
        assert "[Verse 1]" not in result
        assert "Hello world" in result

    def test_removes_genius_header_noise(self):
        raw = "24 ContributorsTranslationsSong Lyrics\n[Verse 1]\nActual lyrics"
        result = clean_lyrics(raw)
        assert "Contributors" not in result
        assert "Actual lyrics" in result

    def test_collapses_blank_lines(self):
        raw = "line 1\n\n\n\n\nline 2"
        result = clean_lyrics(raw)
        assert result == "line 1\n\nline 2"

    def test_plain_text_no_markers(self):
        raw = "Simple lyrics\nwithout markers"
        result = clean_lyrics(raw)
        assert result == "Simple lyrics\nwithout markers"
