"""UVR (Ultimate Vocal Remover) separator wrapper.

Wraps the ``audio-separator`` library for vocal/instrumental separation.
In v3-rc1, supports GPU inference via torch_device parameter and uses
BS-Roformer model for higher quality separation (SDR 12.9).
"""

from __future__ import annotations

import pathlib

import structlog

logger = structlog.get_logger(__name__)


class UVRSeparator:
    """Wrapper around audio-separator for vocal/instrumental separation.

    Args:
        model_cache_dir: Directory where model files are stored.
        media_root: Root directory for all media output.
        model_name: Model filename. Default is BS-Roformer.
        torch_device: 'cuda' or 'cpu'. Default 'cuda'.
    """

    MODEL_NAME = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"

    def __init__(
        self,
        model_cache_dir: str,
        media_root: str,
        model_name: str | None = None,
        torch_device: str = "cuda",
    ) -> None:
        self.model_cache_dir = model_cache_dir
        self.media_root = media_root
        self._model_name = model_name or self.MODEL_NAME
        self.torch_device = torch_device
        self._separator: object | None = None
        self._output_dir: str | None = None

    def _get_separator(self) -> object:
        """Return (and cache) the audio-separator Separator instance."""
        if self._separator is not None:
            return self._separator

        from audio_separator.separator import Separator

        output_dir = pathlib.Path(self.media_root) / "instrumental"
        output_dir.mkdir(parents=True, exist_ok=True)
        self._output_dir = str(output_dir)

        self._separator = Separator(
            output_dir=self._output_dir,
            model_file_dir=self.model_cache_dir,
            output_format="MP3",
        )
        self._separator.load_model(model_filename=self._model_name)

        logger.info(
            "uvr_model_loaded",
            model=self._model_name,
            device=self.torch_device,
            output_dir=self._output_dir,
        )
        return self._separator

    def separate(self, mp3_path: str) -> tuple[str, str]:
        """Separate vocals and instrumental tracks from an MP3 file.

        Runs synchronously — use asyncio.to_thread from async context.

        Args:
            mp3_path: Absolute path to the source MP3 file.

        Returns:
            (vocals_path, instrumental_path) tuple.
        """
        separator = self._get_separator()

        logger.info("uvr_starting", mp3_path=mp3_path)

        output_files: list[str] = separator.separate(mp3_path)

        vocals_path: str | None = None
        instrumental_path: str | None = None

        for path in output_files:
            if not pathlib.Path(path).is_absolute():
                path = str(pathlib.Path(self._output_dir) / path)
            basename_lower = pathlib.Path(path).name.lower()
            if "no_vocal" in basename_lower or "instrument" in basename_lower:
                instrumental_path = path
            elif "vocal" in basename_lower:
                vocals_path = path

        if not vocals_path or not instrumental_path:
            raise RuntimeError(
                f"UVR separation failed: expected vocals + instrumental, "
                f"got {output_files}"
            )

        logger.info(
            "uvr_completed",
            vocals_path=vocals_path,
            instrumental_path=instrumental_path,
        )
        return vocals_path, instrumental_path

    def cleanup(self) -> None:
        """Release GPU memory held by the model."""
        import gc

        if self._separator is not None:
            del self._separator
            self._separator = None

        gc.collect()

        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

        logger.info("uvr_cleanup_done")
