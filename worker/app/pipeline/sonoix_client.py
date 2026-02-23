from __future__ import annotations

import asyncio
import os

import httpx
import structlog
from pydantic import BaseModel

logger = structlog.get_logger(__name__)

# How long to wait between status polls (seconds).
_POLL_INTERVAL_SEC = 1.0

# How long to wait between retries on failure (seconds).
_RETRY_DELAY_SEC = 2.0


class WordToken(BaseModel):
    """One word-level token returned by the Soniox transcription API."""

    text: str
    start_ms: int
    end_ms: int
    confidence: float = 0.0
    language: str | None = None


class TranscriptionResult(BaseModel):
    """Completed transcription with the full text and word-level tokens."""

    full_text: str
    tokens: list[WordToken]
    language: str | None = None  # detected dominant language


class SonoixClient:
    """Async HTTP client for the Soniox Speech-to-Text API.

    The full transcription flow is:
    1. Upload the audio file via POST /v1/files.
    2. Create a transcription job via POST /v1/transcriptions.
    3. Poll GET /v1/transcriptions/{id} until status == 'completed'.
    4. Fetch the transcript via GET /v1/transcriptions/{id}/transcript.
    5. Delete the transcription and uploaded file (fire-and-forget).

    Args:
        api_key: Soniox API key.  Never hard-coded — pass from config/env.
        api_url: Base URL for the Soniox API.
        timeout: Total HTTP request timeout in seconds.
        max_retries: How many times to retry failed API calls before raising.
    """

    def __init__(
        self,
        api_key: str,
        api_url: str = "https://api.soniox.com",
        timeout: float = 120.0,
        max_retries: int = 2,
    ) -> None:
        self._api_key = api_key
        self._api_url = api_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def transcribe(self, audio_path: str) -> TranscriptionResult:
        """Upload audio, transcribe it, and return word-level tokens.

        Args:
            audio_path: Absolute path to the audio file to transcribe.

        Returns:
            TranscriptionResult with full_text and word-level tokens.

        Raises:
            RuntimeError: If the transcription job reports an error status.
            httpx.HTTPStatusError: If any API call returns a non-2xx status.
        """
        # httpx picks up HTTP_PROXY / HTTPS_PROXY automatically from the
        # environment when proxies=None.  We allow an explicit override via
        # the HTTPS_PROXY env var for flexibility.
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")

        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=self._timeout,
            proxy=proxy,
        ) as client:
            file_id = await self._upload_file(client, audio_path)
            logger.info("sonoix_file_uploaded", file_id=file_id, audio_path=audio_path)

            transcription_id = await self._create_transcription(client, file_id)
            logger.info(
                "sonoix_transcription_created",
                transcription_id=transcription_id,
                file_id=file_id,
            )

            await self._wait_for_completion(client, transcription_id)
            logger.info("sonoix_transcription_completed", transcription_id=transcription_id)

            result = await self._fetch_transcript(client, transcription_id)
            logger.info(
                "sonoix_transcript_fetched",
                transcription_id=transcription_id,
                token_count=len(result.tokens),
            )

            # Fire-and-forget cleanup with its own short-lived client so that
            # the main `async with` block can close safely.
            asyncio.create_task(
                self._cleanup(transcription_id, file_id)
            )

            return result

    # ------------------------------------------------------------------
    # Internal steps
    # ------------------------------------------------------------------

    async def _upload_file(self, client: httpx.AsyncClient, audio_path: str) -> str:
        """Upload the audio file and return the Soniox file ID."""
        with open(audio_path, "rb") as audio_file:
            file_bytes = audio_file.read()

        url = f"{self._api_url}/v1/files"
        response = await self._request_with_retry(
            client,
            "POST",
            url,
            files={"file": ("audio.wav", file_bytes, "audio/wav")},
        )
        return response["id"]

    async def _create_transcription(
        self, client: httpx.AsyncClient, file_id: str
    ) -> str:
        """Submit a transcription job and return the transcription ID."""
        url = f"{self._api_url}/v1/transcriptions"
        payload = {
            "model": "stt-async-v4",
            "file_id": file_id,
            "language_hints": ["ru", "en"],
            "enable_language_identification": True,
        }
        response = await self._request_with_retry(client, "POST", url, json=payload)
        return response["id"]

    async def _wait_for_completion(
        self, client: httpx.AsyncClient, transcription_id: str
    ) -> None:
        """Poll until the transcription status is 'completed'.

        Raises RuntimeError if the status becomes 'error'.
        """
        url = f"{self._api_url}/v1/transcriptions/{transcription_id}"

        while True:
            response = await self._request_with_retry(client, "GET", url)
            status = response.get("status", "")

            if status == "completed":
                return

            if status == "error":
                error_message = response.get("error_message", "Unknown transcription error")
                raise RuntimeError(
                    f"Soniox transcription {transcription_id} failed: {error_message}"
                )

            logger.debug(
                "sonoix_poll",
                transcription_id=transcription_id,
                status=status,
            )
            await asyncio.sleep(_POLL_INTERVAL_SEC)

    async def _fetch_transcript(
        self, client: httpx.AsyncClient, transcription_id: str
    ) -> TranscriptionResult:
        """Fetch the transcript and build a TranscriptionResult."""
        url = f"{self._api_url}/v1/transcriptions/{transcription_id}/transcript"
        response = await self._request_with_retry(client, "GET", url)

        full_text = response.get("text", "")
        raw_tokens = response.get("tokens", [])

        tokens = [
            WordToken(
                text=t["text"],
                start_ms=t["start_ms"],
                end_ms=t["end_ms"],
                confidence=t.get("confidence", 0.0),
                language=t.get("language"),
            )
            for t in raw_tokens
            if t.get("text", "").strip()  # skip whitespace-only tokens
        ]

        dominant_language = self._detect_dominant_language(tokens)

        return TranscriptionResult(
            full_text=full_text,
            tokens=tokens,
            language=dominant_language,
        )

    async def _cleanup(
        self,
        transcription_id: str,
        file_id: str,
    ) -> None:
        """Delete the transcription and file from Soniox (best effort).

        Creates its own short-lived httpx client because the caller's
        ``async with`` client may already be closed by the time this task runs.
        """
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        try:
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=30.0,
                proxy=proxy,
            ) as client:
                try:
                    await client.delete(
                        f"{self._api_url}/v1/transcriptions/{transcription_id}"
                    )
                except Exception as exc:
                    logger.warning(
                        "sonoix_cleanup_transcription_failed",
                        transcription_id=transcription_id,
                        error=str(exc),
                    )

                try:
                    await client.delete(f"{self._api_url}/v1/files/{file_id}")
                except Exception as exc:
                    logger.warning(
                        "sonoix_cleanup_file_failed",
                        file_id=file_id,
                        error=str(exc),
                    )
        except Exception as exc:
            logger.warning("sonoix_cleanup_client_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Retry helper
    # ------------------------------------------------------------------

    async def _request_with_retry(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs,
    ) -> dict:
        """Make an HTTP request, retrying up to max_retries times on failure.

        Args:
            client: The active httpx.AsyncClient to use.
            method: HTTP method string ('GET', 'POST', etc.).
            url: Full request URL.
            **kwargs: Additional arguments forwarded to client.request().

        Returns:
            Parsed JSON response body as a dict.

        Raises:
            httpx.HTTPStatusError: After all retries are exhausted.
        """
        last_exception: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = await client.request(method, url, **kwargs)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                # Only retry server errors (5xx); client errors (4xx) are
                # not transient (bad API key, malformed request, etc.).
                if exc.response.status_code < 500:
                    raise
                last_exception = exc
            except httpx.RequestError as exc:
                last_exception = exc

            if attempt < self._max_retries:
                logger.warning(
                    "sonoix_request_retry",
                    attempt=attempt + 1,
                    max_retries=self._max_retries,
                    url=url,
                    error=str(last_exception),
                )
                await asyncio.sleep(_RETRY_DELAY_SEC)

        raise last_exception  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Language detection
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_dominant_language(tokens: list[WordToken]) -> str | None:
        """Return the most frequently occurring language across all tokens."""
        counts: dict[str, int] = {}
        for token in tokens:
            if token.language:
                counts[token.language] = counts.get(token.language, 0) + 1

        if not counts:
            return None

        return max(counts, key=lambda lang: counts[lang])
