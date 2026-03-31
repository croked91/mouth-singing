"""Hybrid CTC forced alignment for syllable-level timing.

All ONNX work runs in a **separate process** via subprocess.Popen to
fully isolate heap corruption that ONNX Runtime can cause.  If the
child crashes, the main worker survives and reports a RuntimeError.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import structlog

from karaoke_shared.models.track import SyllableTiming

logger = structlog.get_logger(__name__)

MIN_FRAMES_FOR_CHAR = 10

# Subprocess timeout (seconds).
_SUBPROCESS_TIMEOUT = 300


@dataclass
class AlignmentStats:
    """Alignment quality statistics."""
    total_words: int = 0
    char_level_used: int = 0
    proportional_fallback: int = 0


class CTCAligner:
    """CTC alignment via isolated subprocess.

    Delegates all ONNX work to worker.common.ctc_subprocess run as a
    standalone Python process.  This avoids fork/spawn issues with
    CUDA-loaded parent processes and isolates potential heap corruption.

    Args:
        syllabifier: Unused (kept for API compat).
        model_cache_dir: Unused (kept for API compat).
        min_frames_for_char: Passed to subprocess.
        device: Unused (subprocess always uses CPU to avoid VRAM contention).
        batch_size: Batch size for generate_emissions in subprocess.
    """

    def __init__(
        self,
        syllabifier=None,
        model_cache_dir: str | None = None,
        min_frames_for_char: int = MIN_FRAMES_FOR_CHAR,
        device: str = "cpu",
        batch_size: int = 16,
    ) -> None:
        self._batch_size = batch_size

        # Eagerly download/cache the ONNX model at startup.
        from ctc_forced_aligner import AlignmentSingleton
        AlignmentSingleton()

        logger.info(
            "ctc_aligner_loaded",
            subprocess=True,
            batch_size=batch_size,
        )

    def align(
        self,
        vocals_path: str,
        lyrics_text: str,
        language: str,
    ) -> tuple[list[SyllableTiming], AlignmentStats]:
        """Align lyrics to audio in a fully isolated subprocess.

        Raises:
            ValueError: If lyrics_text is empty.
            RuntimeError: If subprocess crashes or times out.
        """
        if not lyrics_text or not lyrics_text.strip():
            raise ValueError("lyrics_text is empty")

        logger.info("ctc_alignment_starting", language=language)
        t0 = time.monotonic()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"
            lyrics_path = Path(tmpdir) / "lyrics.txt"
            lyrics_path.write_text(lyrics_text, encoding="utf-8")

            cmd = [
                "python3", "-m", "worker.common.ctc_subprocess",
                "--vocals", vocals_path,
                "--lyrics-file", str(lyrics_path),
                "--language", language,
                "--batch-size", str(self._batch_size),
                "--output", str(output_path),
            ]

            try:
                result = subprocess.run(
                    cmd,
                    timeout=_SUBPROCESS_TIMEOUT,
                    capture_output=True,
                    text=True,
                )
            except subprocess.TimeoutExpired:
                raise RuntimeError(
                    f"CTC alignment subprocess timed out after {_SUBPROCESS_TIMEOUT}s"
                )

            if result.returncode != 0:
                # Check if output file has error details
                if output_path.exists():
                    try:
                        data = json.loads(output_path.read_text())
                        if "error" in data:
                            raise RuntimeError(
                                f"CTC alignment failed: {data['error']}"
                            )
                    except (json.JSONDecodeError, KeyError):
                        pass
                raise RuntimeError(
                    f"CTC alignment subprocess crashed "
                    f"(exit code {result.returncode}). "
                    f"stderr: {result.stderr[-500:]}"
                )

            if not output_path.exists():
                raise RuntimeError("CTC alignment produced no output")

            data = json.loads(output_path.read_text(encoding="utf-8"))

            if "error" in data:
                raise RuntimeError(f"CTC alignment failed: {data['error']}")

            timings = [
                SyllableTiming(
                    syllable=t["syllable"],
                    start=t["start"],
                    end=t["end"],
                )
                for t in data["timings"]
            ]
            stats = AlignmentStats(
                total_words=data["stats"]["total_words"],
                char_level_used=data["stats"]["char_level_used"],
                proportional_fallback=data["stats"]["proportional_fallback"],
            )

            logger.info(
                "alignment_complete",
                total_words=stats.total_words,
                char_level=stats.char_level_used,
                fallback=stats.proportional_fallback,
                syllables=len(timings),
                subprocess=True,
                duration_sec=round(time.monotonic() - t0, 2),
            )
            return timings, stats
