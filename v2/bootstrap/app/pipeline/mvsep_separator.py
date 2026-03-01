"""MVSEP.com API separator for vocal/instrumental separation.

Uses the MVSEP cloud API as an alternative to local UVR models.
Same interface as ``UVRSeparator``: ``separate(mp3_path)`` returns
``(vocals_path, instrumental_path)``.

Karaoke model (sep_type=49) outputs 4 stems:
  - vocals-lead   (lead vocals for WhisperX alignment)
  - vocals-back   (backing vocals, discarded)
  - instrum-only  (clean instrumental for karaoke playback)
  - back-instrum  (backing vocals + instrumental, discarded)
"""

from __future__ import annotations

import pathlib
import time

import requests
import structlog

logger = structlog.get_logger(__name__)

# MVSep Karaoke (lead/back vocals) — render_id=49
_DEFAULT_SEP_TYPE = 49
_OUTPUT_FORMAT_MP3 = 0
_POLL_INTERVAL = 5  # seconds between status checks
_MAX_POLL_TIME = 600  # 10 minutes max wait


class MVSepSeparator:
    """Cloud-based vocal separator using MVSEP.com API.

    Args:
        api_key: MVSEP API token.
        media_root: Root directory for output files.
        sep_type: Algorithm render_id (default 49 = Karaoke lead/back vocals).
    """

    def __init__(
        self,
        api_key: str,
        media_root: str,
        sep_type: int = _DEFAULT_SEP_TYPE,
    ) -> None:
        self._api_key = api_key
        self._media_root = media_root
        self._sep_type = sep_type

    def separate(self, mp3_path: str) -> tuple[str, str]:
        """Submit MP3 to MVSEP, poll for result, download stems.

        Returns:
            A ``(vocals_path, instrumental_path)`` tuple.
            vocals_path = lead vocals stem (for WhisperX alignment).
            instrumental_path = clean instrumental (for karaoke playback).
        """
        output_dir = pathlib.Path(self._media_root) / "instrumental"
        output_dir.mkdir(parents=True, exist_ok=True)

        # --- Submit job ---
        logger.info("mvsep.submitting", mp3_path=mp3_path, sep_type=self._sep_type)
        with open(mp3_path, "rb") as f:
            resp = requests.post(
                "https://mvsep.com/api/separation/create",
                data={
                    "api_token": self._api_key,
                    "sep_type": self._sep_type,
                    "output_format": _OUTPUT_FORMAT_MP3,
                },
                files={"audiofile": (pathlib.Path(mp3_path).name, f, "audio/mpeg")},
                timeout=120,
            )
        resp.raise_for_status()
        result = resp.json()

        if not result.get("success"):
            raise RuntimeError(f"MVSEP submit failed: {result}")

        job_hash = result["data"]["hash"]
        logger.info("mvsep.submitted", hash=job_hash)

        # --- Poll for completion ---
        # Status is at top level: {"success": true, "status": "done", "data": {...}}
        elapsed = 0
        while elapsed < _MAX_POLL_TIME:
            time.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL

            status_resp = requests.get(
                "https://mvsep.com/api/separation/get",
                params={"api_token": self._api_key, "hash": job_hash},
                timeout=30,
            )
            status_resp.raise_for_status()
            status_json = status_resp.json()

            state = status_json.get("status", "unknown")
            if state == "done":
                break
            if state == "failed":
                raise RuntimeError(f"MVSEP job failed: {status_json}")

            logger.debug("mvsep.polling", state=state, elapsed=elapsed)
        else:
            raise RuntimeError(
                f"MVSEP job timed out after {_MAX_POLL_TIME}s: {job_hash}"
            )

        # --- Download stems ---
        files = status_json.get("data", {}).get("files", [])
        logger.info("mvsep.done", file_count=len(files), hash=job_hash)

        vocals_path: str | None = None
        instrumental_path: str | None = None

        stem = pathlib.Path(mp3_path).stem

        for file_info in files:
            url = file_info.get("url", "")
            # name is often None; classify by URL filename instead.
            url_filename = url.rsplit("/", 1)[-1].lower()

            if "vocals-lead" in url_filename or "vocal" in url_filename:
                # Lead vocals — used for WhisperX force_align.
                if "back" not in url_filename:
                    out_path = output_dir / f"{stem}_(Vocals)_mvsep_karaoke.mp3"
                    dl_resp = requests.get(url, timeout=120)
                    dl_resp.raise_for_status()
                    out_path.write_bytes(dl_resp.content)
                    vocals_path = str(out_path)
                    logger.debug("mvsep.downloaded", stem="vocals-lead", path=str(out_path))
            elif "instrum-only" in url_filename:
                # Clean instrumental — karaoke playback.
                out_path = output_dir / f"{stem}_(Instrumental)_mvsep_karaoke.mp3"
                dl_resp = requests.get(url, timeout=120)
                dl_resp.raise_for_status()
                out_path.write_bytes(dl_resp.content)
                instrumental_path = str(out_path)
                logger.debug("mvsep.downloaded", stem="instrum-only", path=str(out_path))
            else:
                # vocals-back, back-instrum — skip download to save bandwidth.
                logger.debug("mvsep.skipped_stem", url_tail=url_filename)

        if not vocals_path or not instrumental_path:
            # Fallback: download all and try to classify.
            raise RuntimeError(
                f"MVSEP: could not identify vocals/instrumental from URLs: "
                f"{[f.get('url', '').rsplit('/', 1)[-1] for f in files]}"
            )

        logger.info(
            "mvsep.completed",
            vocals_path=vocals_path,
            instrumental_path=instrumental_path,
        )
        return vocals_path, instrumental_path

    def cleanup(self) -> None:
        """No-op — cloud API holds no local GPU resources."""
