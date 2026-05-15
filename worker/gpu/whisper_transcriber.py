"""Whisper ASR transcriber for song identification.

Uses HuggingFace Transformers (PyTorch-native) for local speech-to-text.
Accuracy is not critical — the result is used only to identify the song
for LLM lyrics search. Errors in 20-30% of words are acceptable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import structlog

logger = structlog.get_logger(__name__)

MODEL_ID_MAP = {
    "tiny": "openai/whisper-tiny",
    "base": "openai/whisper-base",
    "small": "openai/whisper-small",
    "medium": "openai/whisper-medium",
}


@dataclass
class WhisperResult:
    """ASR transcription result."""

    text: str  # full text, segments joined by ' '
    language: str  # two-letter code ('ru', 'en', ...)


class WhisperTranscriber:
    """PyTorch-native Whisper transcriber via HuggingFace Transformers.

    The model is loaded eagerly at construction; ``cleanup()`` releases
    VRAM and the next ``transcribe()`` call reloads weights from the
    local HuggingFace cache. Methods are synchronous; use
    ``asyncio.to_thread`` for async contexts.

    Args:
        model_size: 'tiny' (~70MB) or 'base' (~140MB).
        device: 'cuda' or 'cpu'.
        compute_type: 'float16' for GPU (selects torch.float16), ignored for CPU.
        model_cache_dir: Directory for HuggingFace model cache.
    """

    def __init__(
        self,
        model_size: str = "medium",
        device: str = "cuda",
        compute_type: str = "float16",
        model_cache_dir: str | None = None,
    ) -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._model_cache_dir = model_cache_dir
        self._model = None
        self._processor = None
        self._torch_dtype = None
        self._load_model()

    def _load_model(self):
        import torch
        from transformers import WhisperForConditionalGeneration, WhisperProcessor

        model_id = MODEL_ID_MAP.get(
            self._model_size, f"openai/whisper-{self._model_size}"
        )

        self._torch_dtype = (
            torch.float16
            if self._device == "cuda" and "16" in self._compute_type
            else torch.float32
        )

        self._processor = WhisperProcessor.from_pretrained(
            model_id,
            cache_dir=self._model_cache_dir,
        )
        self._model = WhisperForConditionalGeneration.from_pretrained(
            model_id,
            cache_dir=self._model_cache_dir,
            dtype=self._torch_dtype,
        ).to(self._device)

        logger.info(
            "whisper_loaded",
            model_size=self._model_size,
            device=self._device,
            backend="transformers",
        )

    def transcribe(self, audio_path: str) -> WhisperResult:
        """Transcribe an audio file.

        Args:
            audio_path: Path to WAV file (ideally 16kHz mono after VAD).

        Returns:
            WhisperResult with text and language.
        """
        import soundfile as sf
        import torch
        import torchaudio.functional as F

        if self._model is None:
            self._load_model()

        logger.info("whisper_starting", audio_path=audio_path)
        t0 = time.monotonic()

        data, sr = sf.read(audio_path, dtype="float32")
        if data.ndim > 1:
            data = data.mean(axis=1)
        if sr != 16000:
            audio = F.resample(torch.from_numpy(data), sr, 16000).numpy()
        else:
            audio = data

        # Process in 30-second chunks (Whisper's native window size)
        chunk_samples = 30 * 16000
        all_text_parts = []
        language = "en"

        for chunk_start in range(0, len(audio), chunk_samples):
            chunk = audio[chunk_start : chunk_start + chunk_samples]

            inputs = self._processor(
                chunk,
                sampling_rate=16000,
                return_tensors="pt",
            )
            input_features = inputs.input_features.to(
                device=self._device,
                dtype=self._torch_dtype,
            )

            with torch.no_grad():
                output = self._model.generate(
                    input_features,
                    return_dict_in_generate=True,
                    max_new_tokens=440,
                )

            token_ids = output.sequences[0]
            chunk_text = self._processor.decode(
                token_ids, skip_special_tokens=True
            ).strip()
            if chunk_text:
                all_text_parts.append(chunk_text)

            # Detect language from first chunk
            if chunk_start == 0:
                first_tokens = self._processor.decode(
                    token_ids[:4], skip_special_tokens=False
                )
                for lang_code in [
                    "ru",
                    "en",
                    "es",
                    "fr",
                    "de",
                    "it",
                    "pt",
                    "zh",
                    "ja",
                    "ko",
                    "uk",
                    "pl",
                    "cs",
                    "tr",
                    "ar",
                    "hi",
                    "th",
                    "vi",
                    "nl",
                    "sv",
                ]:
                    if f"<|{lang_code}|>" in first_tokens:
                        language = lang_code
                        break

        text = " ".join(all_text_parts)

        elapsed = round(time.monotonic() - t0, 2)

        logger.info(
            "whisper_completed",
            language=language,
            text_length=len(text),
            text=text,
            duration_sec=elapsed,
        )

        return WhisperResult(text=text, language=language)

    def cleanup(self) -> None:
        """Release VRAM held by the model."""
        import gc

        del self._model
        del self._processor
        self._model = None
        self._processor = None
        gc.collect()

        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

        logger.info("whisper_cleanup_done")
