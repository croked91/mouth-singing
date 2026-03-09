"""Whisper ASR transcriber for song identification.

Wraps faster-whisper for local speech-to-text. Accuracy is not critical —
the result is used only to identify the song for LLM lyrics search.
Errors in 20-30% of words are acceptable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class WhisperResult:
    """ASR transcription result."""
    text: str           # full text, segments joined by ' '
    language: str       # two-letter code ('ru', 'en', ...)
    confidence: float   # average log-prob → prob (0..1)


class WhisperTranscriber:
    """Wrapper around faster-whisper for local ASR.

    The model is loaded once at construction and held in memory.
    Methods are synchronous; use asyncio.to_thread for async contexts.

    Args:
        model_size: 'tiny' (~70MB, ~5s on T4) or 'base' (~140MB).
        device: 'cuda' or 'cpu'.
        compute_type: 'float16' for GPU, 'int8' for CPU.
        model_cache_dir: Directory for HuggingFace model cache.
    """

    def __init__(
        self,
        model_size: str = "tiny",
        device: str = "cuda",
        compute_type: str = "float16",
        model_cache_dir: str | None = None,
    ) -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._model_cache_dir = model_cache_dir
        self._model = self._load_model()

    def _load_model(self):
        from faster_whisper import WhisperModel

        model = WhisperModel(
            self._model_size,
            device=self._device,
            compute_type=self._compute_type,
            download_root=self._model_cache_dir,
        )
        logger.info("whisper_loaded", model_size=self._model_size, device=self._device)
        return model

    def transcribe(self, audio_path: str) -> WhisperResult:
        """Transcribe an audio file.

        Args:
            audio_path: Path to WAV file (ideally 16kHz mono after VAD).

        Returns:
            WhisperResult with text, language, confidence.
        """
        if self._model is None:
            self._model = self._load_model()

        segments_gen, info = self._model.transcribe(
            audio_path,
            beam_size=1,
            vad_filter=False,
            language=None,
            condition_on_previous_text=False,
            temperature=0.0,
        )

        segments = list(segments_gen)

        if not segments:
            logger.warning("whisper_empty_result", audio_path=audio_path)
            return WhisperResult(text="", language=info.language or "en", confidence=0.0)

        text = " ".join(s.text.strip() for s in segments if s.text.strip())

        avg_logprob = sum(s.avg_logprob for s in segments) / len(segments)
        confidence = min(1.0, max(0.0, math.exp(avg_logprob)))

        logger.info(
            "whisper_completed",
            language=info.language,
            confidence=round(confidence, 3),
            segments=len(segments),
            text_length=len(text),
        )

        return WhisperResult(
            text=text,
            language=info.language or "en",
            confidence=confidence,
        )

    def cleanup(self) -> None:
        """Release VRAM held by the model."""
        import gc

        del self._model
        self._model = None  # type: ignore[assignment]
        gc.collect()

        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

        logger.info("whisper_cleanup_done")
