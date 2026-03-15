"""MVSEP API client for cloud-based stem separation.

Replaces local UVR separation. Uploads an MP3 to the MVSEP API,
polls until the job finishes, downloads vocals and instrumental files.

API flow:
  1. POST /api/separation/create — multipart upload
  2. GET  /api/separation/get?id=JOB_ID — poll until status == "finished"
  3. Download each output file listed in output_files[]
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog

logger = structlog.get_logger(__name__)

# Keywords that indicate the instrumental stem.
# Checked FIRST because some filenames contain both 'no_vocal' and 'vocal'.
_INSTRUMENTAL_KEYWORDS = ("no_vocal", "karaoke", "instrum", "accomp")

# Keywords that indicate the vocals stem (checked after instrumental exclusion).
_VOCAL_KEYWORDS = ("vocal", "voice")


@dataclass
class StemResult:
    """Paths to the downloaded stem files."""

    vocals_path: str        # absolute path to downloaded vocals file
    instrumental_path: str  # absolute path to downloaded instrumental file


class MVSEPError(Exception):
    """Base class for all MVSEP client errors."""


class MVSEPTimeoutError(MVSEPError):
    """Job did not finish within the allowed time."""


class MVSEPAPIError(MVSEPError):
    """Non-retryable API error (4xx responses)."""


class MVSEPJobError(MVSEPError):
    """The MVSEP job itself reported an error status."""


class MVSEPParseError(MVSEPError):
    """Cannot identify vocals or instrumental in the job output files."""


class MVSEPClient:
    """Async client for the MVSEP stem separation API.

    Args:
        api_key: MVSEP API token.
        api_url: Base URL for the MVSEP API.
        sep_type: Separation model type ID (49 = BS-Roformer by default).
        output_format: Output audio format ("mp3", "wav", etc.).
        poll_interval_sec: Seconds to wait between status poll requests.
        timeout_sec: Total seconds to wait for a job before raising timeout.
        max_retries: Number of retries on 5xx / network errors.
        media_root: Root directory for downloaded output files.
    """

    def __init__(
        self,
        api_key: str,
        api_url: str = "https://mvsep.com/api",
        sep_type: int = 49,
        output_format: str = "mp3",
        poll_interval_sec: float = 10.0,
        timeout_sec: float = 600.0,
        max_retries: int = 3,
        media_root: str = "/data/media",
    ) -> None:
        self._api_key = api_key
        self._api_url = api_url.rstrip("/")
        self._sep_type = sep_type
        self._output_format = output_format
        self._poll_interval = poll_interval_sec
        self._timeout_sec = timeout_sec
        self._max_retries = max_retries
        self._output_dir = Path(media_root) / "instrumental"

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def separate(self, mp3_path: str) -> StemResult:
        """Upload mp3_path to MVSEP, wait for completion, download stems.

        Args:
            mp3_path: Absolute path to the source MP3 file.

        Returns:
            StemResult with absolute paths to the vocals and instrumental files.

        Raises:
            MVSEPAPIError: On non-retryable HTTP errors (4xx).
            MVSEPTimeoutError: If the job does not finish within timeout_sec.
            MVSEPJobError: If MVSEP reports the job as failed.
            MVSEPParseError: If vocals or instrumental cannot be identified.
        """
        self._output_dir.mkdir(parents=True, exist_ok=True)

        source = Path(mp3_path)
        base_name = source.stem

        log = logger.bind(mp3=mp3_path, base_name=base_name)
        log.info("mvsep_separate_start")

        job_id = await self._submit_job(source, log)
        log = log.bind(job_id=job_id)
        log.info("mvsep_job_submitted")

        output_files = await self._wait_for_completion(job_id, log)

        result = await self._download_stems(output_files, base_name, log)
        log.info(
            "mvsep_separate_done",
            vocals=result.vocals_path,
            instrumental=result.instrumental_path,
        )
        return result

    # ------------------------------------------------------------------
    # Step 1: Submit the separation job
    # ------------------------------------------------------------------

    async def _submit_job(self, source: Path, log: structlog.BoundLogger) -> str:
        """Upload the audio file and return the MVSEP job ID."""
        url = f"{self._api_url}/separation/create"

        upload_timeout = httpx.Timeout(30.0, read=300.0, write=300.0)

        last_error: Exception | None = None

        for attempt in range(1 + self._max_retries):
            try:
                with source.open("rb") as audio_file:
                    files = {"audiofile": (source.name, audio_file, "audio/mpeg")}
                    data = {
                        "api_token": self._api_key,
                        "sep_type": str(self._sep_type),
                        "add_to_cloud": "0",
                    }

                    async with httpx.AsyncClient(timeout=upload_timeout) as client:
                        resp = await client.post(url, data=data, files=files)

                if resp.status_code >= 500:
                    log.warning(
                        "mvsep_submit_server_error",
                        status=resp.status_code,
                        attempt=attempt,
                    )
                    last_error = MVSEPAPIError(
                        f"Server error {resp.status_code} on submit"
                    )
                    if attempt < self._max_retries:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    raise last_error

                if resp.status_code >= 400:
                    raise MVSEPAPIError(
                        f"Submit failed with HTTP {resp.status_code}: {resp.text[:200]}"
                    )

                body = resp.json()
                if not body.get("success"):
                    raise MVSEPAPIError(
                        f"MVSEP rejected the job: {body}"
                    )

                job_id: str = body["data"].get("id") or body["data"]["hash"]
                return job_id

            except (MVSEPAPIError, MVSEPError):
                raise
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning(
                    "mvsep_submit_network_error",
                    error=str(exc),
                    attempt=attempt,
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(2 ** attempt)
                    continue

        raise MVSEPAPIError(f"Submit failed after all retries: {last_error}")

    # ------------------------------------------------------------------
    # Step 2: Poll until the job finishes
    # ------------------------------------------------------------------

    async def _wait_for_completion(
        self, job_id: str, log: structlog.BoundLogger,
    ) -> list[dict]:
        """Poll the job status endpoint until finished, then return output_files.

        Args:
            job_id: The MVSEP job identifier returned by the submit call.
            log: Bound logger carrying context for this job.

        Returns:
            The output_files list from the finished job response.

        Raises:
            MVSEPTimeoutError: If the job does not finish within timeout_sec.
            MVSEPJobError: If MVSEP reports the job as failed.
            MVSEPAPIError: On persistent HTTP errors while polling.
        """
        url = f"{self._api_url}/separation/get"
        params = {"hash": job_id, "api_token": self._api_key}
        poll_timeout = httpx.Timeout(15.0, read=30.0, write=10.0)

        start_time = time.monotonic()
        poll_count = 0

        while True:
            elapsed = time.monotonic() - start_time
            if elapsed >= self._timeout_sec:
                raise MVSEPTimeoutError(
                    f"Job {job_id} did not finish within {self._timeout_sec:.0f}s"
                )

            await asyncio.sleep(self._poll_interval)
            poll_count += 1

            status_data = await self._fetch_job_status(
                url, params, poll_timeout, log, poll_count,
            )

            job_status = status_data.get("status", "")
            log.debug(
                "mvsep_poll",
                status=job_status,
                poll=poll_count,
                elapsed_sec=round(elapsed, 1),
            )

            if job_status in ("finished", "done"):
                data = status_data.get("data", status_data)
                output_files = (
                    data.get("files")
                    or data.get("output_files")
                    or status_data.get("output_files")
                    or []
                )
                if not output_files:
                    raise MVSEPJobError(
                        f"Job {job_id} finished but no output files found"
                    )
                return output_files

            if job_status == "error":
                error_msg = status_data.get("error_message", "unknown error")
                raise MVSEPJobError(
                    f"Job {job_id} failed on MVSEP: {error_msg}"
                )

            # Any other status ("pending", "processing", etc.) — keep polling.

    async def _fetch_job_status(
        self,
        url: str,
        params: dict,
        timeout: httpx.Timeout,
        log: structlog.BoundLogger,
        poll_count: int,
    ) -> dict:
        """Make one poll request and return the parsed JSON body.

        Retries on 5xx and network errors; raises MVSEPAPIError on 4xx.
        """
        last_error: Exception | None = None

        for attempt in range(1 + self._max_retries):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.get(url, params=params)

                if resp.status_code >= 500:
                    last_error = MVSEPAPIError(
                        f"Poll server error {resp.status_code}"
                    )
                    log.warning(
                        "mvsep_poll_server_error",
                        status=resp.status_code,
                        poll=poll_count,
                        attempt=attempt,
                    )
                    if attempt < self._max_retries:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    raise last_error

                if resp.status_code >= 400:
                    raise MVSEPAPIError(
                        f"Poll failed with HTTP {resp.status_code}: {resp.text[:200]}"
                    )

                return resp.json()

            except (MVSEPAPIError, MVSEPError):
                raise
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning(
                    "mvsep_poll_network_error",
                    error=str(exc),
                    poll=poll_count,
                    attempt=attempt,
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(2 ** attempt)
                    continue

        raise MVSEPAPIError(f"Poll failed after all retries: {last_error}")

    # ------------------------------------------------------------------
    # Step 3: Download stems
    # ------------------------------------------------------------------

    async def _download_stems(
        self,
        output_files: list[dict],
        base_name: str,
        log: structlog.BoundLogger,
    ) -> StemResult:
        """Classify and download vocals and instrumental from output_files.

        Args:
            output_files: List of dicts with 'filename' and 'large_path' keys.
            base_name: Stem of the source MP3 filename (without extension).
            log: Bound logger carrying context for this job.

        Returns:
            StemResult with absolute local paths to the two downloaded files.

        Raises:
            MVSEPParseError: If vocals or instrumental cannot be identified.
        """
        vocals_entry = None
        instrumental_entry = None

        for entry in output_files:
            # MVSEP returns either {type, url} or {filename, large_path}
            label = (
                entry.get("type", "")
                or entry.get("filename", "")
                or entry.get("url", "")
            ).lower()
            if _is_instrumental(label):
                instrumental_entry = entry
            elif _is_vocals(label):
                vocals_entry = entry

        if vocals_entry is None:
            labels = [e.get("type") or e.get("filename", "") for e in output_files]
            raise MVSEPParseError(
                f"Cannot identify vocals file in output_files: {labels}"
            )

        if instrumental_entry is None:
            labels = [e.get("type") or e.get("filename", "") for e in output_files]
            raise MVSEPParseError(
                f"Cannot identify instrumental file in output_files: {labels}"
            )

        vocals_url = vocals_entry.get("url") or vocals_entry.get("large_path")
        instrumental_url = instrumental_entry.get("url") or instrumental_entry.get("large_path")

        ext = Path(vocals_url).suffix.lstrip(".") if vocals_url else "mp3"
        vocals_path = self._output_dir / f"{base_name}_vocals.{ext}"
        instrumental_path = self._output_dir / f"{base_name}_instrumental.{ext}"

        log.info(
            "mvsep_downloading",
            vocals_url=vocals_url,
            instrumental_url=instrumental_url,
        )

        # Download both files concurrently.
        await asyncio.gather(
            self._download_file(vocals_url, vocals_path, log),
            self._download_file(instrumental_url, instrumental_path, log),
        )

        return StemResult(
            vocals_path=str(vocals_path),
            instrumental_path=str(instrumental_path),
        )

    async def _download_file(
        self,
        url: str,
        dest: Path,
        log: structlog.BoundLogger,
    ) -> None:
        """Stream a file from url to dest on disk.

        Uses streaming to handle large files without loading them into memory.
        Retries on network errors and 5xx responses.

        Args:
            url: Direct download URL for the file.
            dest: Destination Path on the local filesystem.
            log: Bound logger carrying context for this job.
        """
        download_timeout = httpx.Timeout(30.0, read=300.0, write=10.0)
        last_error: Exception | None = None

        for attempt in range(1 + self._max_retries):
            try:
                async with httpx.AsyncClient(
                    timeout=download_timeout, follow_redirects=True,
                ) as client:
                    async with client.stream("GET", url) as resp:
                        if resp.status_code >= 500:
                            last_error = MVSEPAPIError(
                                f"Download server error {resp.status_code}"
                            )
                            log.warning(
                                "mvsep_download_server_error",
                                url=url,
                                status=resp.status_code,
                                attempt=attempt,
                            )
                            if attempt < self._max_retries:
                                await asyncio.sleep(2 ** attempt)
                                continue
                            raise last_error

                        if resp.status_code >= 400:
                            raise MVSEPAPIError(
                                f"Download failed with HTTP {resp.status_code}: {url}"
                            )

                        with dest.open("wb") as out_file:
                            async for chunk in resp.aiter_bytes(chunk_size=65536):
                                out_file.write(chunk)

                log.info("mvsep_download_done", dest=str(dest))
                return

            except (MVSEPAPIError, MVSEPError):
                raise
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning(
                    "mvsep_download_network_error",
                    url=url,
                    error=str(exc),
                    attempt=attempt,
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(2 ** attempt)
                    continue

        raise MVSEPAPIError(
            f"Download of {url} failed after all retries: {last_error}"
        )


# ------------------------------------------------------------------
# Stem classification helpers
# ------------------------------------------------------------------

def _is_instrumental(filename: str) -> bool:
    """Return True if the filename looks like an instrumental stem.

    Checked before _is_vocals because some instrumental filenames contain
    the word 'vocal' (e.g. 'no_vocal', 'no_vocals').
    """
    return any(keyword in filename for keyword in _INSTRUMENTAL_KEYWORDS)


def _is_vocals(filename: str) -> bool:
    """Return True if the filename looks like a vocals stem.

    Only called after _is_instrumental has already returned False.
    """
    return any(keyword in filename for keyword in _VOCAL_KEYWORDS)
