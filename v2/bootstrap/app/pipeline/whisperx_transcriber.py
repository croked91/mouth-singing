"""WhisperX transcription and forced alignment wrapper.

WhisperX is an optional dependency — the module can be imported even when
WhisperX is not installed.  Attempting to instantiate ``WhisperXTranscriber``
without WhisperX present will raise a clear ``ImportError``.

Word-level output format returned by both public methods::

    [{"word": "hello", "start": 1.23, "end": 1.56}, ...]
"""

from __future__ import annotations

from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

try:
    import whisperx

    HAS_WHISPERX = True
except ImportError:
    HAS_WHISPERX = False


def _require_whisperx() -> None:
    """Raise a clear error if WhisperX is not installed."""
    if not HAS_WHISPERX:
        raise ImportError(
            "WhisperX is not installed. Install it with:\n"
            "    pip install whisperx torch torchaudio\n"
            "or use the [whisperx] extra:\n"
            "    pip install karaoke-bootstrap[whisperx]"
        )


class WhisperXTranscriber:
    """Wraps WhisperX for full transcription and forced alignment.

    The alignment model (wav2vec2, lightweight) is loaded eagerly at
    construction time.  The heavy ASR model is loaded lazily on the first
    ``transcribe()`` call so that force-align-only usage avoids the cost.

    Args:
        model_name: Whisper model size (e.g. "medium", "large-v3").
        language: BCP-47 language code for transcription and alignment (e.g.
            "ru", "en").
        device: PyTorch device string ("cpu" or "cuda").
    """

    def __init__(
        self,
        model_name: str = "medium",
        language: str = "ru",
        device: str = "cpu",
    ) -> None:
        _require_whisperx()

        self._language = language
        self._device = device
        self._model_name = model_name
        self._asr_model = None  # loaded lazily on first transcribe()

        logger.info(
            "whisperx.loading_align_model",
            language=language,
            device=device,
        )

        # Load the alignment model for word-level timestamps.
        self._align_model, self._align_metadata = whisperx.load_align_model(
            language_code=language,
            device=device,
        )

        logger.info("whisperx.align_model_ready")

    def _ensure_asr_model(self) -> None:
        """Load the ASR model on first use."""
        if self._asr_model is not None:
            return

        logger.info(
            "whisperx.loading_asr_model",
            model=self._model_name,
            device=self._device,
        )

        self._asr_model = whisperx.load_model(
            self._model_name,
            device=self._device,
            compute_type="int8",
            language=self._language,
        )

        logger.info("whisperx.asr_model_ready", model=self._model_name)

    def transcribe(self, audio_path: Path) -> list[dict]:
        """Transcribe an audio file and return word-level timestamps.

        Runs the full WhisperX pipeline: VAD chunking, ASR, and then forced
        alignment to obtain per-word start/end times.

        Args:
            audio_path: Path to the audio file (any format supported by
                ffmpeg / librosa).

        Returns:
            List of word dicts: ``[{"word": str, "start": float, "end": float}, ...]``.
            Words without alignment confidence may be absent from the output.

        Raises:
            ImportError: If WhisperX is not installed.
            RuntimeError: If transcription produces no segments.
        """
        _require_whisperx()
        self._ensure_asr_model()

        logger.info("whisperx.transcribing", audio_path=str(audio_path))

        audio = whisperx.load_audio(str(audio_path))

        # Transcribe with WhisperX (returns segments with word timestamps).
        raw_result = self._asr_model.transcribe(audio, batch_size=16)

        if not raw_result.get("segments"):
            logger.warning("whisperx.no_segments", audio_path=str(audio_path))
            return []

        # Force-align to get precise word-level timestamps.
        aligned = whisperx.align(
            raw_result["segments"],
            self._align_model,
            self._align_metadata,
            audio,
            self._device,
            return_char_alignments=False,
        )

        return self._extract_words(aligned)

    def force_align(
        self, audio_path: Path, segments: list[dict]
    ) -> list[dict]:
        """Force-align pre-segmented text to an audio file.

        Each segment must have ``text``, ``start`` (seconds), and ``end``
        (seconds).  Typically one segment per LRC line.

        Args:
            audio_path: Path to the audio file.
            segments: List of ``{"text": str, "start": float, "end": float}``.

        Returns:
            List of word dicts: ``[{"word": str, "start": float, "end": float}, ...]``.

        Raises:
            ImportError: If WhisperX is not installed.
        """
        _require_whisperx()

        logger.info(
            "whisperx.force_aligning",
            audio_path=str(audio_path),
            segment_count=len(segments),
        )

        audio = whisperx.load_audio(str(audio_path))

        aligned = whisperx.align(
            segments,
            self._align_model,
            self._align_metadata,
            audio,
            self._device,
            return_char_alignments=False,
        )

        return self._extract_words(aligned)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_words(aligned_result: dict) -> list[dict]:
        """Flatten aligned WhisperX segments into a list of word dicts.

        Args:
            aligned_result: The dict returned by ``whisperx.align()``.

        Returns:
            List of ``{"word": str, "start": float, "end": float}`` dicts.
            Words missing start or end timestamps are skipped.
        """
        words: list[dict] = []

        for segment in aligned_result.get("segments", []):
            for word_info in segment.get("words", []):
                word_text = word_info.get("word", "").strip()
                start = word_info.get("start")
                end = word_info.get("end")

                if not word_text or start is None or end is None:
                    continue

                words.append({"word": word_text, "start": float(start), "end": float(end)})

        return words
