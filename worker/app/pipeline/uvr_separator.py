"""UVR (Ultimate Vocal Remover) separator wrapper.

Wraps the ``audio-separator`` library for vocal/instrumental separation using
the UVR-MDX-NET-Voc_FT ONNX model. The library is imported lazily inside
``separate()`` because it is a heavy dependency only present in the worker
Docker image — importing it at module load time would break any environment
that only has the shared package installed.
"""

from __future__ import annotations

import pathlib

import structlog

logger = structlog.get_logger(__name__)


class UVRSeparator:
    """Wrapper around audio-separator for vocal/instrumental separation.

    Args:
        model_cache_dir: Directory where the ONNX model file is stored.
        media_root: Root directory for all media output.
    """

    MODEL_NAME = "UVR-MDX-NET-Voc_FT.onnx"

    def __init__(self, model_cache_dir: str, media_root: str) -> None:
        self.model_cache_dir = model_cache_dir
        self.media_root = media_root
        self._separator: object | None = None
        self._output_dir: str | None = None

    def _get_separator(self) -> object:
        """Return (and cache) the audio-separator Separator instance.

        The model is loaded once on first call and reused for subsequent
        separation jobs, avoiding ~1–3s of ONNX model loading overhead
        per invocation.
        """
        if self._separator is not None:
            return self._separator

        from audio_separator.separator import Separator  # noqa: PLC0415

        output_dir = pathlib.Path(self.media_root) / "instrumental"
        output_dir.mkdir(parents=True, exist_ok=True)
        self._output_dir = str(output_dir)

        self._separator = Separator(
            output_dir=self._output_dir,
            model_file_dir=self.model_cache_dir,
        )
        self._separator.load_model(model_filename=self.MODEL_NAME)

        logger.info(
            "uvr_model_loaded",
            model=self.MODEL_NAME,
            output_dir=self._output_dir,
        )
        return self._separator

    def separate(self, mp3_path: str) -> tuple[str, str]:
        """Separate vocals and instrumental tracks from an MP3 file.

        Runs synchronously and is intended to be called via
        ``asyncio.to_thread`` from an async context so it does not block the
        event loop.

        The ``audio_separator`` library is imported lazily via
        ``_get_separator()`` because it has a large dependency footprint
        (ONNX, NumPy, etc.) and is only installed inside the worker Docker
        image.

        Args:
            mp3_path: Absolute path to the source MP3 file.

        Returns:
            A ``(vocals_path, instrumental_path)`` tuple with the absolute
            paths to the two output files produced by the separator.

        Raises:
            RuntimeError: If the separator does not produce exactly one vocals
                file and one instrumental file.
        """
        separator = self._get_separator()

        logger.info("uvr_starting", mp3_path=mp3_path)

        # Returns a list of absolute output file paths.
        output_files: list[str] = separator.separate(mp3_path)

        vocals_path: str | None = None
        instrumental_path: str | None = None

        for path in output_files:
            lower = path.lower()
            # Check "no_vocal" before "vocal" — "no_vocal" contains the
            # substring "vocal", so the order of checks matters.
            if "no_vocal" in lower or "instrument" in lower:
                instrumental_path = path
            elif "vocal" in lower:
                vocals_path = path

        if not vocals_path or not instrumental_path:
            raise RuntimeError(
                f"UVR separation failed: expected a vocals file and an "
                f"instrumental file, got {output_files}"
            )

        logger.info(
            "uvr_completed",
            vocals_path=vocals_path,
            instrumental_path=instrumental_path,
        )

        return vocals_path, instrumental_path
