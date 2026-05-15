"""Unit tests for LyricsAgent: tool-wrapper enforcement + graceful exhaustion."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from worker.common.lyrics.filename_parser import ParsedFilename, _build_variants
from worker.common.lyrics.provider_chain import _variants
from worker.common.lyrics_agent import (
    LyricsAgent,
    _MAX_CONSECUTIVE_SEARCHES,
    _quoted_phrase_too_long,
    _web_search,
)


# ======================================================================
# _quoted_phrase_too_long — pure validator
# ======================================================================


class TestQuotedPhraseValidator:
    def test_no_quotes_returns_none(self):
        assert _quoted_phrase_too_long("Dzetta lyrics text") is None

    def test_short_quote_one_word_returns_none(self):
        assert _quoted_phrase_too_long('Dzetta "Кометы" lyrics') is None

    def test_three_word_quote_returns_none(self):
        assert _quoted_phrase_too_long('"один два три" lyrics') is None

    def test_long_quote_returns_phrase(self):
        bad = _quoted_phrase_too_long(
            'lyrics "очень длинная фраза из пяти слов" текст'
        )
        assert bad == "очень длинная фраза из пяти слов"

    def test_multiple_quotes_one_too_long(self):
        bad = _quoted_phrase_too_long(
            '"кит" lyrics "слишком длинная фраза в кавычках"'
        )
        assert bad == "слишком длинная фраза в кавычках"


# ======================================================================
# _web_search — quoted-phrase guard before HTTP
# ======================================================================


class TestWebSearchValidation:
    def test_long_quote_blocks_http_call(self):
        with patch("worker.common.lyrics_agent.httpx.get") as mock_get:
            result = _web_search(
                'lyrics "очень длинная фраза из пяти слов"',
                backend="searxng",
                searxng_url="http://searxng:8080",
                timeout=5.0,
            )
        mock_get.assert_not_called()
        data = json.loads(result)
        assert "error" in data
        assert "слишком длинная" in data["error"]

    def test_searxng_backend_uses_only_searxng(self):
        with (
            patch(
                "worker.common.lyrics_agent._searxng_search",
                return_value=[{"title": "t", "href": "u", "body": "b"}],
            ) as mock_sx,
            patch("worker.common.lyrics_agent._yandex_search") as mock_yx,
        ):
            result = _web_search(
                "Dzetta Кометы",
                backend="searxng",
                api_key="key",
                folder_id="folder",
                timeout=5.0,
                searxng_url="http://searxng:8080",
            )
        mock_sx.assert_called_once()
        mock_yx.assert_not_called()
        assert json.loads(result)[0]["href"] == "u"

    def test_yandex_backend_uses_only_yandex(self):
        with (
            patch("worker.common.lyrics_agent._searxng_search") as mock_sx,
            patch(
                "worker.common.lyrics_agent._yandex_search",
                return_value=[{"title": "y", "href": "v", "body": "b"}],
            ) as mock_yx,
        ):
            result = _web_search(
                "Dzetta Кометы",
                backend="yandex",
                api_key="key",
                folder_id="folder",
                timeout=5.0,
                searxng_url="http://searxng:8080",
            )
        mock_sx.assert_not_called()
        mock_yx.assert_called_once()
        assert json.loads(result)[0]["href"] == "v"

    def test_returns_not_configured_error_when_backend_missing_creds(self):
        result = _web_search("q", backend="yandex", api_key="", folder_id="")
        assert "not configured" in json.loads(result)["error"]

    def test_returns_no_results_error(self):
        with patch(
            "worker.common.lyrics_agent._searxng_search", return_value=None,
        ):
            result = _web_search(
                "q", backend="searxng",
                searxng_url="http://searxng:8080", timeout=5.0,
            )
        assert "Ничего не найдено" in json.loads(result)["error"]


# ======================================================================
# _run_agent — consecutive web_search guard + graceful exhaustion
# ======================================================================


def _mock_chat_response(content=None, tool_calls=None):
    message = MagicMock()
    message.content = content
    message.tool_calls = tool_calls
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    return response


def _mock_tool_call(name: str, arguments: dict, call_id: str = "c1"):
    tc = MagicMock()
    tc.function.name = name
    tc.function.arguments = json.dumps(arguments)
    tc.id = call_id
    return tc


def _make_agent(max_iterations=20):
    return LyricsAgent(
        deepseek_api_key="sk-test",
        searxng_url="http://searxng:8080",
        max_iterations=max_iterations,
    )


class TestConsecutiveSearchGuard:
    def test_third_consecutive_search_is_blocked(self):
        """Two web_search are allowed, third must be intercepted."""
        agent = _make_agent(max_iterations=10)

        # Three consecutive web_search responses, then a final empty array.
        responses = [
            _mock_chat_response(tool_calls=[_mock_tool_call("web_search", {"query": "q1"}, "c1")]),
            _mock_chat_response(tool_calls=[_mock_tool_call("web_search", {"query": "q2"}, "c2")]),
            _mock_chat_response(tool_calls=[_mock_tool_call("web_search", {"query": "q3"}, "c3")]),
            _mock_chat_response(content="[]"),
        ]

        with (
            patch("worker.common.lyrics_agent.OpenAI") as mock_openai_cls,
            patch(
                "worker.common.lyrics_agent._web_search",
                return_value='[{"title":"t","href":"u","body":"b"}]',
            ) as mock_search,
        ):
            client = MagicMock()
            mock_openai_cls.return_value = client
            client.chat.completions.create.side_effect = responses

            agent._run_agent("user message", "searxng")

        # Only first two web_search calls actually run; third intercepted.
        assert mock_search.call_count == _MAX_CONSECUTIVE_SEARCHES

        # Inspect the third tool result message — it must be the guard error.
        # Captured via the call_args of the chat client (4th call's messages).
        all_calls = client.chat.completions.create.call_args_list
        # Last call's messages contain all prior history including 3 tool results.
        final_messages = all_calls[-1].kwargs["messages"]
        tool_msgs = [m for m in final_messages if isinstance(m, dict) and m.get("role") == "tool"]
        assert len(tool_msgs) == 3
        last_tool = json.loads(tool_msgs[-1]["content"])
        assert "error" in last_tool
        assert "fetch_webpage" in last_tool["error"]

    def test_fetch_webpage_resets_consecutive_counter(self):
        """After fetch_webpage, agent can do web_search again."""
        agent = _make_agent(max_iterations=10)

        responses = [
            _mock_chat_response(tool_calls=[_mock_tool_call("web_search", {"query": "q1"}, "c1")]),
            _mock_chat_response(tool_calls=[_mock_tool_call("web_search", {"query": "q2"}, "c2")]),
            _mock_chat_response(tool_calls=[_mock_tool_call("fetch_webpage", {"url": "http://x"}, "c3")]),
            _mock_chat_response(tool_calls=[_mock_tool_call("web_search", {"query": "q3"}, "c4")]),
            _mock_chat_response(content="[]"),
        ]

        with (
            patch("worker.common.lyrics_agent.OpenAI") as mock_openai_cls,
            patch(
                "worker.common.lyrics_agent._web_search",
                return_value='[{"title":"t","href":"u","body":"b"}]',
            ) as mock_search,
            patch(
                "worker.common.lyrics_agent._fetch_webpage",
                return_value="page content",
            ) as mock_fetch,
        ):
            client = MagicMock()
            mock_openai_cls.return_value = client
            client.chat.completions.create.side_effect = responses

            agent._run_agent("user message", "searxng")

        # All three web_search calls run because fetch reset the counter.
        assert mock_search.call_count == 3
        assert mock_fetch.call_count == 1


class TestGracefulExhaustion:
    def test_exhausted_iterations_returns_empty_array(self):
        """When agent never returns final content, _run_agent returns '[]'."""
        agent = _make_agent(max_iterations=3)

        # Endless tool-call loop. Each response has fetch_webpage to keep
        # the consecutive-search guard from interfering.
        endless = _mock_chat_response(
            tool_calls=[_mock_tool_call("fetch_webpage", {"url": "http://x"}, "c1")]
        )

        with (
            patch("worker.common.lyrics_agent.OpenAI") as mock_openai_cls,
            patch(
                "worker.common.lyrics_agent._fetch_webpage",
                return_value="some page",
            ),
        ):
            client = MagicMock()
            mock_openai_cls.return_value = client
            client.chat.completions.create.side_effect = [endless] * 5

            result = agent._run_agent("user message", "searxng")

        assert result == "[]"

    async def test_search_returns_empty_list_on_exhaustion(self):
        """Public search() returns [] (not raises) on exhaustion."""
        agent = _make_agent(max_iterations=3)
        with patch.object(agent, "_run_agent", return_value="[]"):
            candidates = await agent.search(
                asr_text="some text",
                detected_language="ru",
            )
        assert candidates == []


class TestSequentialBackendPasses:
    """Two-pass: SearXNG first, then Yandex only if SearXNG produced nothing."""

    @staticmethod
    def _agent_with_both_backends(max_iterations: int = 5):
        return LyricsAgent(
            deepseek_api_key="sk-test",
            searxng_url="http://searxng:8080",
            yandex_search_api_key="key",
            yandex_search_folder_id="folder",
            max_iterations=max_iterations,
        )

    async def test_searxng_success_skips_yandex(self):
        agent = self._agent_with_both_backends()
        calls: list[str] = []

        def fake_run(user_message: str, backend: str, _language: str = "en") -> str:  # noqa: ARG001
            calls.append(backend)
            return json.dumps([
                {"artist": "A", "title": "T", "lyrics": "lyrics text long enough"}
            ])

        with patch.object(agent, "_run_agent", side_effect=fake_run):
            candidates = await agent.search(
                asr_text="some text", detected_language="ru",
            )

        assert calls == ["searxng"]  # Yandex never invoked
        assert len(candidates) == 1

    async def test_searxng_empty_falls_through_to_yandex(self):
        agent = self._agent_with_both_backends()
        calls: list[str] = []

        def fake_run(user_message: str, backend: str, _language: str = "en") -> str:  # noqa: ARG001
            calls.append(backend)
            if backend == "searxng":
                return "[]"
            return json.dumps([
                {"artist": "A", "title": "T", "lyrics": "lyrics text from yandex pass"}
            ])

        with patch.object(agent, "_run_agent", side_effect=fake_run):
            candidates = await agent.search(
                asr_text="some text", detected_language="ru",
            )

        assert calls == ["searxng", "yandex"]
        assert len(candidates) == 1

    async def test_yandex_pass_message_includes_hint(self):
        agent = self._agent_with_both_backends()
        captured_messages: list[str] = []

        def fake_run(user_message: str, backend: str, _language: str = "en") -> str:  # noqa: ARG001
            captured_messages.append(user_message)
            return "[]"

        with patch.object(agent, "_run_agent", side_effect=fake_run):
            await agent.search(asr_text="some text", detected_language="ru")

        # Two passes
        assert len(captured_messages) == 2
        # First pass: clean
        assert "Системная подсказка" not in captured_messages[0]
        # Second pass: hint about prior failure included
        assert "Системная подсказка" in captured_messages[1]
        assert "searxng" in captured_messages[1].lower()

    async def test_only_searxng_configured_single_pass(self):
        agent = LyricsAgent(
            deepseek_api_key="sk-test",
            searxng_url="http://searxng:8080",  # no yandex creds
            max_iterations=3,
        )
        calls: list[str] = []

        def fake_run(user_message: str, backend: str, _language: str = "en") -> str:  # noqa: ARG001
            calls.append(backend)
            return "[]"

        with patch.object(agent, "_run_agent", side_effect=fake_run):
            await agent.search(asr_text="x", detected_language="ru")

        assert calls == ["searxng"]

    async def test_no_backends_returns_empty(self):
        agent = LyricsAgent(deepseek_api_key="sk-test", searxng_url=None)
        with patch.object(agent, "_run_agent") as mock_run:
            candidates = await agent.search(asr_text="x", detected_language="ru")
        assert candidates == []
        mock_run.assert_not_called()


# ======================================================================
# ParsedFilename + _build_variants — filename parser variant logic
# ======================================================================


class TestBuildVariants:
    def test_canonical_only_when_no_original(self):
        assert _build_variants("Coldplay", None) == ("Coldplay",)

    def test_returns_both_when_different(self):
        assert _build_variants("Джетта", "Dzetta") == ("Джетта", "Dzetta")

    def test_dedupe_case_insensitive(self):
        assert _build_variants("Coldplay", "coldplay") == ("Coldplay",)

    def test_strips_whitespace(self):
        assert _build_variants("  Coldplay  ", " Coldplay ") == ("Coldplay",)

    def test_empty_returns_empty(self):
        assert _build_variants(None, None) == ()
        assert _build_variants("", "") == ()

    def test_only_original_promoted_to_primary(self):
        # If canonical missing (LLM omitted it), original is used as the
        # sole variant — better than dropping the only signal we have.
        assert _build_variants(None, "Dzetta") == ("Dzetta",)


class TestParsedFilenameProperties:
    def test_single_variant(self):
        p = ParsedFilename(artist_variants=("Coldplay",), title_variants=("Yellow",))
        assert p.artist == "Coldplay"
        assert p.artist_alts == []
        assert p.title == "Yellow"
        assert p.title_alts == []

    def test_two_variants(self):
        p = ParsedFilename(
            artist_variants=("Джетта", "Dzetta"),
            title_variants=("Кометы",),
        )
        assert p.artist == "Джетта"
        assert p.artist_alts == ["Dzetta"]
        assert p.title == "Кометы"

    def test_empty(self):
        e = ParsedFilename.empty()
        assert e.artist is None
        assert e.title is None
        assert e.artist_alts == []
        assert e.title_alts == []


# ======================================================================
# provider_chain._variants — combine primary + alts
# ======================================================================


class TestVariantsHelper:
    def test_primary_only(self):
        assert _variants("Джетта", None) == ["Джетта"]

    def test_primary_and_alts(self):
        assert _variants("Джетта", ["Dzetta"]) == ["Джетта", "Dzetta"]

    def test_dedupe_case_insensitive(self):
        assert _variants("Coldplay", ["coldplay", "COLDPLAY"]) == ["Coldplay"]

    def test_empty_primary(self):
        assert _variants(None, None) == []
        assert _variants("", []) == []

    def test_alts_only_when_primary_none(self):
        # Behavior: when primary is None, alts are still returned.
        assert _variants(None, ["Dzetta"]) == ["Dzetta"]

    def test_skips_blank_alts(self):
        assert _variants("Джетта", ["", " ", "Dzetta"]) == ["Джетта", "Dzetta"]


# ======================================================================
# LyricsAgent.search — alts forwarded to user_message
# ======================================================================


class TestSearchAltsInPrompt:
    async def test_alts_appear_in_user_message(self):
        agent = _make_agent(max_iterations=3)
        captured: dict[str, str] = {}

        def fake_run(user_message: str, backend: str, _language: str = "en") -> str:  # noqa: ARG001
            captured["msg"] = user_message
            return "[]"

        with patch.object(agent, "_run_agent", side_effect=fake_run):
            await agent.search(
                asr_text="some text",
                detected_language="ru",
                artist_hint="Джетта",
                title_hint="Кометы",
                artist_alts=["Dzetta"],
                title_alts=[],
            )

        assert "Джетта" in captured["msg"]
        assert "Dzetta" in captured["msg"]
        assert "Альтернативные написания исполнителя" in captured["msg"]
        # title_alts empty → no title-alts line.
        assert "Альтернативные написания названия" not in captured["msg"]

    async def test_no_alts_no_extra_lines(self):
        agent = _make_agent(max_iterations=3)
        captured: dict[str, str] = {}

        def fake_run(user_message: str, backend: str, _language: str = "en") -> str:  # noqa: ARG001
            captured["msg"] = user_message
            return "[]"

        with patch.object(agent, "_run_agent", side_effect=fake_run):
            await agent.search(
                asr_text="some text",
                detected_language="ru",
                artist_hint="Coldplay",
                title_hint="Yellow",
            )

        assert "Альтернативные написания" not in captured["msg"]


# ======================================================================
# LyricsProviderChain — ASR fallback when nothing matches
# ======================================================================


class TestAsrFallback:
    """Verify chain falls back to raw ASR text when matcher rejects all."""

    @staticmethod
    def _make_chain():
        from worker.common.lyrics import LyricsProviderChain
        from worker.common.lyrics.matching import LyricsExpander, LyricsMatcher

        return LyricsProviderChain(
            text_providers=[],
            metadata_providers=[],
            matcher=LyricsMatcher(
                expander=LyricsExpander(deepseek_api_key=None),
                deepseek_api_key=None,
            ),
            filename_parser=None,
            fallback_agent=None,
        )

    async def test_returns_asr_fallback_when_no_candidates(self):
        chain = self._make_chain()
        # No providers, no fallback agent → straight to ASR fallback.
        asr = (
            "тёплое место но улицы ждут отпечатков наших ног "
            "звёздная пыль на сапогах мягкая"
        )
        result = await chain.search(
            asr_text=asr,
            detected_language="ru",
            artist_hint="Кино",
            title_hint="Звезда",
        )
        assert result.source_note == "asr_fallback"
        assert result.confidence == "low"
        assert result.artist == "Кино"
        assert result.title == "Звезда"
        assert result.lyrics == asr
        assert result.language == "ru"

    async def test_asr_fallback_uses_unknown_when_no_hints(self):
        chain = self._make_chain()
        asr = "some long enough text that exceeds the twenty char minimum"
        result = await chain.search(
            asr_text=asr, detected_language="en",
        )
        assert result.artist == "Unknown"
        assert result.title == "Unknown"
        assert result.lyrics == asr

    async def test_raises_when_asr_too_short(self):
        from worker.common.lyrics_searcher import LyricsNotFoundError

        chain = self._make_chain()
        with pytest.raises(LyricsNotFoundError, match="too short"):
            await chain.search(
                asr_text="hi",  # < 20 chars
                detected_language="en",
            )

    async def test_raises_when_asr_empty(self):
        from worker.common.lyrics_searcher import LyricsNotFoundError

        chain = self._make_chain()
        with pytest.raises(LyricsNotFoundError, match="too short"):
            await chain.search(asr_text="", detected_language="en")
