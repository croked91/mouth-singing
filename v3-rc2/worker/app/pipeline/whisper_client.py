"""OpenAI Whisper API client for song ASR transcription.

Replaces local faster-whisper (v3-rc1) with the hosted Whisper API.
Used only for song identification — transcript accuracy is not critical.

Handles:
- Automatic language detection via verbose_json response format.
- File size enforcement (25 MB limit) with ffmpeg compression fallback.
- Retry logic for rate limits, server errors, and network failures.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import httpx
import structlog

logger = structlog.get_logger(__name__)

_WHISPER_ENDPOINT = "https://api.openai.com/v1/audio/transcriptions"
_MAX_FILE_BYTES = 25 * 1024 * 1024  # 25 MB

# OpenAI returns language as a full English word ("russian", "english", etc.).
# Map to ISO 639-1. For unlisted languages, we fall back to taking the first
# two characters of the word, which works for most European languages.
_LANG_MAP: dict[str, str] = {
    "russian": "ru",
    "english": "en",
    "ukrainian": "uk",
    "german": "de",
    "french": "fr",
    "spanish": "es",
    "italian": "it",
    "portuguese": "pt",
    "japanese": "ja",
    "chinese": "zh",
    "korean": "ko",
}


@dataclass
class WhisperResult:
    """ASR transcription result from the OpenAI Whisper API."""

    text: str
    language: str | None  # ISO 639-1 code ("ru", "en", ...) or None


class WhisperAPIError(Exception):
    """Base class for Whisper API errors."""


class WhisperAuthError(WhisperAPIError):
    """Raised on HTTP 401 — bad or missing API key."""


class WhisperAPIClient:
    """Async client for the OpenAI Whisper transcription API.

    Args:
        api_key: OpenAI API key.
        model: Whisper model name. Only "whisper-1" is currently available.
        timeout: HTTP request timeout in seconds.
        max_retries: How many additional attempts to make after the first failure.
        language_hint: ISO 639-1 language code to pass as a hint. Empty string
            means auto-detect (recommended for karaoke).
    """

    def __init__(
        self,
        api_key: str,
        model: str = "whisper-1",
        timeout: float = 120.0,
        max_retries: int = 2,
        language_hint: str = "",
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._max_retries = max_retries
        self._language_hint = language_hint

    async def transcribe(self, audio_path: str) -> WhisperResult:
        """Transcribe an audio file using the OpenAI Whisper API.

        Automatically compresses the file if it exceeds the 25 MB limit.
        Retries on rate limits, server errors, and transient network failures.

        Args:
            audio_path: Absolute path to the audio file.

        Returns:
            WhisperResult with transcribed text and detected language.

        Raises:
            WhisperAuthError: On HTTP 401 (invalid API key).
            WhisperAPIError: On other unrecoverable errors or exhausted retries.
        """
        upload_path = audio_path
        compressed_path: str | None = None

        try:
            file_size = os.path.getsize(audio_path)
            if file_size > _MAX_FILE_BYTES:
                logger.info(
                    "whisper_file_too_large_compressing",
                    audio_path=audio_path,
                    size_mb=round(file_size / 1024 / 1024, 1),
                )
                compressed_path = await self._compress_audio(audio_path)
                upload_path = compressed_path

            return await self._transcribe_with_retries(upload_path)

        finally:
            if compressed_path is not None and os.path.exists(compressed_path):
                os.remove(compressed_path)
                logger.debug("whisper_temp_deleted", path=compressed_path)

    async def _transcribe_with_retries(self, audio_path: str) -> WhisperResult:
        """Send the transcription request, retrying on transient failures."""
        last_error: Exception | None = None

        for attempt in range(1 + self._max_retries):
            try:
                return await self._send_request(audio_path, attempt)
            except _RateLimitError:
                logger.warning("whisper_rate_limited", attempt=attempt)
                if attempt < self._max_retries:
                    await asyncio.sleep(60.0)
                    continue
                raise WhisperAPIError(
                    f"Whisper rate limited after {attempt + 1} attempts"
                )
            except _ServerError as exc:
                logger.warning(
                    "whisper_server_error", status=exc.status_code, attempt=attempt
                )
                last_error = exc
                if attempt < self._max_retries:
                    backoff = 2.0 ** (attempt + 1)  # 2s, 4s
                    await asyncio.sleep(backoff)
                    continue
            except httpx.HTTPError as exc:
                logger.warning(
                    "whisper_network_error", error=str(exc), attempt=attempt
                )
                last_error = exc
                if attempt < self._max_retries:
                    backoff = 2.0 ** (attempt + 1)
                    await asyncio.sleep(backoff)
                    continue

        raise WhisperAPIError(f"All retries exhausted: {last_error}")

    async def _send_request(self, audio_path: str, attempt: int) -> WhisperResult:
        """Send a single POST request to the transcription endpoint.

        Raises:
            WhisperAuthError: On HTTP 401.
            _RateLimitError: On HTTP 429.
            _ServerError: On HTTP 5xx.
            WhisperAPIError: On other HTTP errors or unexpected response shape.
            httpx.HTTPError: On network-level failures.
        """
        headers = {"Authorization": f"Bearer {self._api_key}"}

        with open(audio_path, "rb") as audio_file:
            file_name = os.path.basename(audio_path)
            form_data: dict[str, str] = {
                "model": self._model,
                "response_format": "verbose_json",
            }
            if self._language_hint:
                form_data["language"] = self._language_hint

            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    _WHISPER_ENDPOINT,
                    headers=headers,
                    data=form_data,
                    files={"file": (file_name, audio_file, "audio/mpeg")},
                )

        logger.debug(
            "whisper_response_received",
            status=resp.status_code,
            attempt=attempt,
        )

        if resp.status_code == 401:
            raise WhisperAuthError("OpenAI API key is invalid or missing")

        if resp.status_code == 429:
            raise _RateLimitError()

        if resp.status_code >= 500:
            raise _ServerError(resp.status_code)

        if resp.status_code >= 400:
            raise WhisperAPIError(
                f"Whisper API error {resp.status_code}: {resp.text[:300]}"
            )

        return self._parse_response(resp.json())

    @staticmethod
    def _parse_response(data: dict) -> WhisperResult:
        """Parse the verbose_json response into a WhisperResult.

        Args:
            data: Decoded JSON response body.

        Returns:
            WhisperResult with text and ISO 639-1 language code.

        Raises:
            WhisperAPIError: If the response shape is unexpected.
        """
        try:
            text = data["text"]
        except KeyError as exc:
            raise WhisperAPIError(
                f"Unexpected Whisper response shape: missing 'text' key"
            ) from exc

        raw_language: str | None = data.get("language")
        language = _resolve_language(raw_language)

        logger.info(
            "whisper_transcribed",
            language=language,
            raw_language=raw_language,
            text_length=len(text),
        )

        return WhisperResult(text=text, language=language)

    @staticmethod
    async def _compress_audio(audio_path: str) -> str:
        """Compress audio to fit the 25 MB API limit.

        Runs ffmpeg to re-encode at 64 kbps mono MP3.

        Args:
            audio_path: Path to the original audio file.

        Returns:
            Path to the temporary compressed file. The caller is responsible
            for deleting it.

        Raises:
            WhisperAPIError: If ffmpeg exits with a non-zero return code.
        """
        out_path = audio_path + ".compressed.mp3"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", audio_path, "-b:a", "64k", "-ac", "1", "-y", out_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

        if proc.returncode != 0:
            raise WhisperAPIError(
                f"ffmpeg compression failed with return code {proc.returncode}"
            )

        compressed_size = os.path.getsize(out_path)
        logger.info(
            "whisper_compression_done",
            out_path=out_path,
            size_mb=round(compressed_size / 1024 / 1024, 1),
        )
        return out_path


# ======================================================================
# Helpers
# ======================================================================


def _resolve_language(raw: str | None) -> str | None:
    """Convert a full language name from the API to an ISO 639-1 code.

    Args:
        raw: Language string from the API (e.g. "russian"), or None.

    Returns:
        ISO 639-1 code (e.g. "ru"), a best-effort two-char fallback, or None.
    """
    if not raw:
        return None

    normalised = raw.strip().lower()
    if normalised in _LANG_MAP:
        return _LANG_MAP[normalised]

    # Best-effort fallback: first two characters usually match ISO 639-1
    # for European languages not in the map (e.g. "polish" → "po").
    fallback = normalised[:2]
    logger.debug(
        "whisper_unknown_language_code",
        raw_language=raw,
        fallback=fallback,
    )
    return fallback


class _RateLimitError(Exception):
    """Internal sentinel for HTTP 429 — never escapes this module."""


class _ServerError(Exception):
    """Internal sentinel for HTTP 5xx — never escapes this module."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"Server error {status_code}")
        self.status_code = status_code
