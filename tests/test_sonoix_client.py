# LEGACY: This file tests the Sonoix transcription API integration which was
# removed in v3-rc1. The Sonoix client (worker/app/pipeline/sonoix_client.py)
# no longer exists in the restructured codebase. These tests are kept for
# historical reference only and will be skipped automatically because the
# module load at the top of the file will raise an AssertionError.
# TODO: Delete this file or replace with tests for WhisperTranscriber.

"""Unit tests for SonoixClient.

Strategy
--------
- Imports SonoixClient via importlib to avoid clobbering the backend ``app``
  namespace (the worker also has an ``app`` package).
- All HTTP calls are mocked with unittest.mock.AsyncMock / patch so no real
  network traffic occurs.
- The audio file upload is patched via ``builtins.open`` so no real file is
  needed on disk.
- asyncio.sleep is patched to keep polling tests fast.
- Table-driven tests for language detection and retry counting.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# ---------------------------------------------------------------------------
# Load worker module under a private namespace
# ---------------------------------------------------------------------------

_WORKER_PIPELINE_DIR = (
    pathlib.Path(__file__).parent.parent / "worker" / "app" / "pipeline"
)

_spec = importlib.util.spec_from_file_location(
    "_sonoix_module",
    str(_WORKER_PIPELINE_DIR / "sonoix_client.py"),
    submodule_search_locations=[],
)
assert _spec is not None and _spec.loader is not None
_sonoix_mod = importlib.util.module_from_spec(_spec)
sys.modules["_sonoix_module"] = _sonoix_mod
_spec.loader.exec_module(_sonoix_mod)

SonoixClient = _sonoix_mod.SonoixClient
WordToken = _sonoix_mod.WordToken
TranscriptionResult = _sonoix_mod.TranscriptionResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(
    api_key: str = "test-key",
    api_url: str = "https://api.test.invalid",
    max_retries: int = 2,
) -> SonoixClient:
    return SonoixClient(
        api_key=api_key,
        api_url=api_url,
        timeout=5.0,
        max_retries=max_retries,
    )


def _mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    """Return a mock httpx.Response that yields json_data from .json()."""
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()  # no-op on 200
    return resp


def _error_response(status_code: int = 500) -> MagicMock:
    """Return a mock httpx.Response that raises HTTPStatusError on raise_for_status."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "error", request=MagicMock(), response=resp
    )
    return resp


# Minimal transcript payload — two tokens with different languages
_TRANSCRIPT_PAYLOAD = {
    "text": "hello world",
    "tokens": [
        {"text": "hello", "start_ms": 0, "end_ms": 500, "confidence": 0.99, "language": "en"},
        {"text": "world", "start_ms": 600, "end_ms": 1100, "confidence": 0.97, "language": "en"},
    ],
}


# ---------------------------------------------------------------------------
# Happy-path: full flow (upload → create → poll → transcript → cleanup)
# ---------------------------------------------------------------------------


class TestSonoixClientHappyPath:
    """Verify the complete transcription flow with all mocked HTTP calls."""

    async def test_transcribe_returns_transcription_result(
        self, tmp_path: pathlib.Path
    ) -> None:
        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"fake-wav-data")

        client = _make_client()

        # Build the sequence of responses for client.request():
        # 1. POST /v1/files → {"id": "file-1"}
        # 2. POST /v1/transcriptions → {"id": "tx-1"}
        # 3. GET /v1/transcriptions/tx-1 → {"status": "completed"}
        # 4. GET /v1/transcriptions/tx-1/transcript → {...}
        responses = [
            _mock_response({"id": "file-1"}),
            _mock_response({"id": "tx-1"}),
            _mock_response({"status": "completed"}),
            _mock_response(_TRANSCRIPT_PAYLOAD),
        ]

        mock_request = AsyncMock(side_effect=responses)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_http_client = AsyncMock()
            mock_http_client.request = mock_request
            mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
            mock_http_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_http_client

            result = await client.transcribe(str(audio_file))

        assert isinstance(result, TranscriptionResult)

    async def test_transcribe_returns_full_text(self, tmp_path: pathlib.Path) -> None:
        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"fake")

        client = _make_client()
        responses = [
            _mock_response({"id": "file-1"}),
            _mock_response({"id": "tx-1"}),
            _mock_response({"status": "completed"}),
            _mock_response(_TRANSCRIPT_PAYLOAD),
        ]
        mock_request = AsyncMock(side_effect=responses)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.request = mock_request
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_http

            result = await client.transcribe(str(audio_file))

        assert result.full_text == "hello world"

    async def test_transcribe_returns_word_tokens(self, tmp_path: pathlib.Path) -> None:
        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"fake")

        client = _make_client()
        responses = [
            _mock_response({"id": "file-1"}),
            _mock_response({"id": "tx-1"}),
            _mock_response({"status": "completed"}),
            _mock_response(_TRANSCRIPT_PAYLOAD),
        ]
        mock_request = AsyncMock(side_effect=responses)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.request = mock_request
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_http

            result = await client.transcribe(str(audio_file))

        assert len(result.tokens) == 2
        assert result.tokens[0].text == "hello"
        assert result.tokens[1].text == "world"

    async def test_transcribe_detects_language(self, tmp_path: pathlib.Path) -> None:
        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"fake")

        client = _make_client()
        responses = [
            _mock_response({"id": "file-1"}),
            _mock_response({"id": "tx-1"}),
            _mock_response({"status": "completed"}),
            _mock_response(_TRANSCRIPT_PAYLOAD),
        ]
        mock_request = AsyncMock(side_effect=responses)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.request = mock_request
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_http

            result = await client.transcribe(str(audio_file))

        assert result.language == "en"

    async def test_transcribe_polls_until_completed(
        self, tmp_path: pathlib.Path
    ) -> None:
        """If the first poll returns 'queued', it should keep polling."""
        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"fake")

        client = _make_client()
        responses = [
            _mock_response({"id": "file-1"}),
            _mock_response({"id": "tx-1"}),
            _mock_response({"status": "queued"}),    # first poll
            _mock_response({"status": "processing"}),  # second poll
            _mock_response({"status": "completed"}),   # third poll
            _mock_response(_TRANSCRIPT_PAYLOAD),
        ]
        mock_request = AsyncMock(side_effect=responses)

        with (
            patch("httpx.AsyncClient") as mock_cls,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_http = AsyncMock()
            mock_http.request = mock_request
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_http

            result = await client.transcribe(str(audio_file))

        # Should have polled 3 times before reaching 'completed'
        assert mock_request.call_count == 6  # upload + create + 3 polls + transcript


# ---------------------------------------------------------------------------
# Error: transcription job reports error status
# ---------------------------------------------------------------------------


class TestSonoixClientErrorStatus:
    async def test_error_status_raises_runtime_error(
        self, tmp_path: pathlib.Path
    ) -> None:
        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"fake")

        client = _make_client()
        responses = [
            _mock_response({"id": "file-1"}),
            _mock_response({"id": "tx-1"}),
            _mock_response({"status": "error", "error_message": "Codec not supported"}),
        ]
        mock_request = AsyncMock(side_effect=responses)

        with (
            patch("httpx.AsyncClient") as mock_cls,
            pytest.raises(RuntimeError, match="Codec not supported"),
        ):
            mock_http = AsyncMock()
            mock_http.request = mock_request
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_http

            await client.transcribe(str(audio_file))

    async def test_error_status_with_default_message(
        self, tmp_path: pathlib.Path
    ) -> None:
        """If error_message is absent the client uses a default message."""
        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"fake")

        client = _make_client()
        responses = [
            _mock_response({"id": "file-1"}),
            _mock_response({"id": "tx-1"}),
            _mock_response({"status": "error"}),  # no error_message key
        ]
        mock_request = AsyncMock(side_effect=responses)

        with (
            patch("httpx.AsyncClient") as mock_cls,
            pytest.raises(RuntimeError),
        ):
            mock_http = AsyncMock()
            mock_http.request = mock_request
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_http

            await client.transcribe(str(audio_file))


# ---------------------------------------------------------------------------
# Retry behaviour on network errors
# ---------------------------------------------------------------------------


class TestSonoixClientRetries:
    async def test_retries_on_request_error(self, tmp_path: pathlib.Path) -> None:
        """On a network error the client retries up to max_retries times.

        With max_retries=2 and the first call always failing, the client
        will attempt the request 3 times total (attempt 0, 1, 2) before
        re-raising.
        """
        client = _make_client(max_retries=2)

        # Make every request raise a network error
        network_error = httpx.ConnectError("Connection refused")
        mock_request = AsyncMock(side_effect=network_error)

        with (
            patch("httpx.AsyncClient") as mock_cls,
            patch("asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(httpx.ConnectError),
        ):
            mock_http = AsyncMock()
            mock_http.request = mock_request
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_http

            # Use a non-existent file — the mock intercepts before open() matters.
            # Actually _upload_file opens the file first; patch builtins.open too.
            with patch("builtins.open", MagicMock(return_value=MagicMock(
                __enter__=MagicMock(return_value=MagicMock(read=MagicMock(return_value=b"x"))),
                __exit__=MagicMock(return_value=False),
            ))):
                await client.transcribe("/fake/audio.wav")

        # max_retries=2 → 3 total attempts for the first request (file upload)
        assert mock_request.call_count == 3

    async def test_succeeds_after_transient_failure(
        self, tmp_path: pathlib.Path
    ) -> None:
        """The client succeeds when the first attempt fails but the second succeeds."""
        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"fake")

        client = _make_client(max_retries=2)

        network_error = httpx.ConnectError("transient")

        # First call to upload fails; second call succeeds, then rest succeed.
        side_effects = [
            network_error,
            _mock_response({"id": "file-1"}),
            _mock_response({"id": "tx-1"}),
            _mock_response({"status": "completed"}),
            _mock_response(_TRANSCRIPT_PAYLOAD),
        ]
        mock_request = AsyncMock(side_effect=side_effects)

        with (
            patch("httpx.AsyncClient") as mock_cls,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_http = AsyncMock()
            mock_http.request = mock_request
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_http

            result = await client.transcribe(str(audio_file))

        assert isinstance(result, TranscriptionResult)

    async def test_zero_retries_fails_immediately(
        self, tmp_path: pathlib.Path
    ) -> None:
        """With max_retries=0 a single failure raises immediately."""
        client = _make_client(max_retries=0)

        network_error = httpx.ConnectError("refused")
        mock_request = AsyncMock(side_effect=network_error)

        with (
            patch("httpx.AsyncClient") as mock_cls,
            patch("asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(httpx.ConnectError),
        ):
            mock_http = AsyncMock()
            mock_http.request = mock_request
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_http

            with patch("builtins.open", MagicMock(return_value=MagicMock(
                __enter__=MagicMock(return_value=MagicMock(read=MagicMock(return_value=b"x"))),
                __exit__=MagicMock(return_value=False),
            ))):
                await client.transcribe("/fake/audio.wav")

        # Only 1 attempt
        assert mock_request.call_count == 1


# ---------------------------------------------------------------------------
# Dominant language detection
# ---------------------------------------------------------------------------


class TestDetectDominantLanguage:
    """Tests for the static _detect_dominant_language helper."""

    @pytest.mark.parametrize(
        "languages, expected",
        [
            (["en", "en", "en"], "en"),
            (["ru", "ru", "en"], "ru"),
            (["en", "ru", "en", "en"], "en"),
            (["ru"], "ru"),
            (["en"], "en"),
        ],
    )
    def test_dominant_language(
        self, languages: list[str], expected: str
    ) -> None:
        tokens = [
            WordToken(text=f"w{i}", start_ms=i * 100, end_ms=(i + 1) * 100, language=lang)
            for i, lang in enumerate(languages)
        ]

        result = SonoixClient._detect_dominant_language(tokens)

        assert result == expected

    def test_all_none_language_returns_none(self) -> None:
        tokens = [
            WordToken(text="a", start_ms=0, end_ms=100, language=None),
            WordToken(text="b", start_ms=100, end_ms=200, language=None),
        ]

        result = SonoixClient._detect_dominant_language(tokens)

        assert result is None

    def test_empty_tokens_returns_none(self) -> None:
        result = SonoixClient._detect_dominant_language([])

        assert result is None

    def test_mixed_none_and_language(self) -> None:
        """None-language tokens are ignored; the majority non-None language wins."""
        tokens = [
            WordToken(text="a", start_ms=0, end_ms=100, language=None),
            WordToken(text="b", start_ms=100, end_ms=200, language="ru"),
            WordToken(text="c", start_ms=200, end_ms=300, language="ru"),
            WordToken(text="d", start_ms=300, end_ms=400, language=None),
        ]

        result = SonoixClient._detect_dominant_language(tokens)

        assert result == "ru"


# ---------------------------------------------------------------------------
# Empty tokens
# ---------------------------------------------------------------------------


class TestEmptyTokens:
    async def test_empty_tokens_returns_empty_result(
        self, tmp_path: pathlib.Path
    ) -> None:
        """If the transcript has no tokens, TranscriptionResult.tokens is empty."""
        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"fake")

        client = _make_client()

        empty_transcript = {"text": "", "tokens": []}
        responses = [
            _mock_response({"id": "file-1"}),
            _mock_response({"id": "tx-1"}),
            _mock_response({"status": "completed"}),
            _mock_response(empty_transcript),
        ]
        mock_request = AsyncMock(side_effect=responses)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.request = mock_request
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_http

            result = await client.transcribe(str(audio_file))

        assert result.tokens == []
        assert result.full_text == ""
        assert result.language is None

    async def test_whitespace_only_tokens_are_filtered(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Tokens with whitespace-only text are filtered out."""
        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"fake")

        client = _make_client()

        transcript_with_spaces = {
            "text": "hello world",
            "tokens": [
                {"text": "hello", "start_ms": 0, "end_ms": 500, "language": "en"},
                {"text": "  ", "start_ms": 500, "end_ms": 600, "language": None},  # whitespace
                {"text": "world", "start_ms": 600, "end_ms": 1100, "language": "en"},
            ],
        }
        responses = [
            _mock_response({"id": "file-1"}),
            _mock_response({"id": "tx-1"}),
            _mock_response({"status": "completed"}),
            _mock_response(transcript_with_spaces),
        ]
        mock_request = AsyncMock(side_effect=responses)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.request = mock_request
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_http

            result = await client.transcribe(str(audio_file))

        # Whitespace-only token is excluded
        assert len(result.tokens) == 2
        assert all(t.text.strip() for t in result.tokens)


# ---------------------------------------------------------------------------
# WordToken and TranscriptionResult model validation
# ---------------------------------------------------------------------------


class TestWordTokenModel:
    def test_word_token_fields(self) -> None:
        token = WordToken(
            text="hello",
            start_ms=0,
            end_ms=500,
            confidence=0.95,
            language="en",
        )

        assert token.text == "hello"
        assert token.start_ms == 0
        assert token.end_ms == 500
        assert token.confidence == 0.95
        assert token.language == "en"

    def test_word_token_defaults(self) -> None:
        token = WordToken(text="hi", start_ms=100, end_ms=200)

        assert token.confidence == 0.0
        assert token.language is None


class TestTranscriptionResultModel:
    def test_transcription_result_fields(self) -> None:
        tokens = [WordToken(text="hello", start_ms=0, end_ms=500)]
        result = TranscriptionResult(full_text="hello", tokens=tokens, language="en")

        assert result.full_text == "hello"
        assert len(result.tokens) == 1
        assert result.language == "en"

    def test_transcription_result_language_optional(self) -> None:
        result = TranscriptionResult(full_text="", tokens=[])

        assert result.language is None


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------


class TestSonoixClientConstruction:
    def test_default_api_url(self) -> None:
        client = SonoixClient(api_key="key")
        assert "soniox.com" in client._api_url

    def test_custom_api_url_trailing_slash_stripped(self) -> None:
        client = SonoixClient(api_key="key", api_url="https://custom.api.test/")
        assert not client._api_url.endswith("/")

    def test_max_retries_stored(self) -> None:
        client = SonoixClient(api_key="key", max_retries=5)
        assert client._max_retries == 5

    def test_api_key_stored(self) -> None:
        client = SonoixClient(api_key="secret-key-123")
        assert client._api_key == "secret-key-123"
