"""GPU-accelerated CTC forced alignment via torchaudio.

Uses MMS-300M forced aligner (315M params, 1130 languages) with native
CUDA forced_align() kernel.  Runs in-process — no subprocess isolation
needed (PyTorch doesn't have ONNX's heap corruption issues).
"""

from __future__ import annotations

import gc
import time
from dataclasses import dataclass

import structlog
import torch
import torchaudio

from karaoke_shared.models.track import SyllableTiming
from karaoke_shared.utils.syllabifier import Syllabifier

logger = structlog.get_logger(__name__)

_SAMPLE_RATE = 16_000
_HF_MODEL_ID = "MahmoudAshraf/mms-300m-1130-forced-aligner"


@dataclass
class AlignmentStats:
    """Alignment quality statistics (matches CTCAligner interface)."""

    total_words: int = 0
    char_level_used: int = 0
    proportional_fallback: int = 0


class TorchCTCAligner:
    """GPU CTC aligner using torchaudio MMS_FA pipeline.

    The model is loaded lazily on first ``align()`` call so that VRAM
    is not occupied during earlier pipeline steps (UVR, Whisper).

    Args:
        device: Torch device string ('cuda' or 'cpu').
        model_cache_dir: HuggingFace cache directory for model weights.
    """

    def __init__(
        self,
        device: str = "cuda",
        model_cache_dir: str | None = None,
    ) -> None:
        self._device = device
        self._cache_dir = model_cache_dir
        self._model = None
        self._bundle = None
        self._dictionary: dict[str, int] = {}
        self._syllabifier = Syllabifier()
        logger.info("torch_ctc_aligner_created", device=device)

    # ------------------------------------------------------------------
    # Lazy model loading
    # ------------------------------------------------------------------

    def _ensure_model(self) -> None:
        """Load MMS-300M forced aligner on first use."""
        if self._model is not None:
            return

        t0 = time.monotonic()
        from transformers import Wav2Vec2ForCTC, Wav2Vec2Processor

        cache_kwargs = {}
        if self._cache_dir:
            cache_kwargs["cache_dir"] = self._cache_dir

        self._model = Wav2Vec2ForCTC.from_pretrained(
            _HF_MODEL_ID, torch_dtype=torch.float16, **cache_kwargs,
        )
        self._model.to(self._device).eval()

        # Build dictionary from processor vocab.
        # Vocab: <blank>=0, <pad>=1, </s>=2, <unk>=3, a=4, ..., x=30
        processor = Wav2Vec2Processor.from_pretrained(
            _HF_MODEL_ID, **cache_kwargs,
        )
        vocab = processor.tokenizer.get_vocab()
        # Keep only single-char alphabetic tokens + apostrophe.
        self._dictionary = {
            k: v for k, v in vocab.items()
            if len(k) == 1 and (k.isalpha() or k == "'")
        }
        self._blank_idx = vocab.get("<blank>", 0)

        logger.info(
            "torch_ctc_model_loaded",
            model=_HF_MODEL_ID,
            device=self._device,
            vocab_size=len(self._dictionary),
            params_m=round(sum(p.numel() for p in self._model.parameters()) / 1e6),
            duration_sec=round(time.monotonic() - t0, 2),
        )

    # ------------------------------------------------------------------
    # Public API (matches CTCAligner.align signature)
    # ------------------------------------------------------------------

    def align(
        self,
        vocals_path: str,
        lyrics_text: str,
        language: str,
    ) -> tuple[list[SyllableTiming], AlignmentStats]:
        """Align lyrics to vocals using GPU-accelerated CTC.

        Raises:
            ValueError: If lyrics_text is empty.
            RuntimeError: If alignment fails.
        """
        if not lyrics_text or not lyrics_text.strip():
            raise ValueError("lyrics_text is empty")

        self._ensure_model()

        logger.info("ctc_alignment_starting", language=language, device=self._device)
        t0 = time.monotonic()

        # 1. Load audio as 16kHz mono.
        waveform = self._load_audio(vocals_path)

        # 2. Generate emissions — full audio, single forward pass on GPU.
        #    HuggingFace Wav2Vec2ForCTC returns .logits (not a tuple).
        with torch.inference_mode():
            output = self._model(waveform.to(device=self._device, dtype=torch.float16))
            # forced_align expects float32 emissions.
            emission = torch.log_softmax(output.logits.float(), dim=-1)

        # 3. Tokenize lyrics (romanize → filter → index).
        words, transcript, first_flags = self._tokenize_lyrics(lyrics_text, language)
        if not transcript:
            raise RuntimeError("No valid tokens after text preprocessing")

        # 4. Build flat token list (excluding blank-mapped chars).
        tokenized = [
            self._dictionary[c]
            for word in transcript
            for c in word
            if c in self._dictionary and self._dictionary[c] != 0
        ]
        if not tokenized:
            raise RuntimeError("All tokens mapped to blank")

        # 5. Run forced alignment on GPU.
        targets = torch.tensor([tokenized], dtype=torch.int64).to(emission.device)
        aligned_tokens, scores = torchaudio.functional.forced_align(
            emission, targets, blank=0,
        )

        # 6. Merge frame-level tokens into spans.
        token_spans = torchaudio.functional.merge_tokens(
            aligned_tokens[0], scores[0],
        )

        # 7. Group token spans into word spans.
        word_lengths = [len(word) for word in transcript]
        word_spans = self._unflatten(token_spans, word_lengths)

        # 8. Convert to timestamps.
        n_frames = emission.size(1)
        ratio = waveform.size(1) / _SAMPLE_RATE / n_frames  # sec per frame

        # 9. Build syllable timings.
        timings, stats = self._to_syllable_timings(
            words, word_spans, ratio, language, first_flags,
        )

        logger.info(
            "alignment_complete",
            total_words=stats.total_words,
            char_level=stats.char_level_used,
            fallback=stats.proportional_fallback,
            syllables=len(timings),
            duration_sec=round(time.monotonic() - t0, 2),
        )
        return timings, stats

    def cleanup(self) -> None:
        """Release VRAM."""
        if self._model is not None:
            del self._model
            self._model = None
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            logger.info("torch_ctc_cleanup_done")

    # ------------------------------------------------------------------
    # Internal: audio loading
    # ------------------------------------------------------------------

    def _load_audio(self, path: str) -> torch.Tensor:
        """Load audio as 16 kHz mono tensor."""
        import librosa

        data, _ = librosa.load(path, sr=_SAMPLE_RATE, mono=True)
        return torch.from_numpy(data).unsqueeze(0)  # (1, samples)

    # ------------------------------------------------------------------
    # Internal: text preprocessing
    # ------------------------------------------------------------------

    def _tokenize_lyrics(
        self, lyrics_text: str, language: str,
    ) -> tuple[list[str], list[list[str]], list[bool]]:
        """Preprocess and tokenize lyrics into word-level char lists.

        Returns:
            words: Original words for display.
            transcript: List of char-lists per word (romanized, filtered).
            is_first_in_line: True for the first word of each lyrics line.
        """
        from unidecode import unidecode

        words_out: list[str] = []
        transcript_out: list[list[str]] = []
        first_flags: list[bool] = []

        for line in lyrics_text.splitlines():
            line_words = line.split()
            if not line_words:
                continue
            is_first_word = True
            for word in line_words:
                cleaned = word.strip()
                if not cleaned:
                    continue

                # Romanize non-Latin text.
                romanized = unidecode(cleaned).lower()
                # Keep only characters in dictionary with non-blank index.
                chars = [
                    c for c in romanized
                    if c in self._dictionary and self._dictionary[c] != 0
                ]
                if not chars:
                    continue

                words_out.append(cleaned)
                transcript_out.append(chars)
                first_flags.append(is_first_word)
                is_first_word = False

        return words_out, transcript_out, first_flags

    # ------------------------------------------------------------------
    # Internal: span grouping
    # ------------------------------------------------------------------

    @staticmethod
    def _unflatten(token_spans: list, word_lengths: list[int]) -> list:
        """Group flat token spans into per-word span lists."""
        result = []
        offset = 0
        for length in word_lengths:
            if offset + length > len(token_spans):
                break
            result.append(token_spans[offset : offset + length])
            offset += length
        return result

    # ------------------------------------------------------------------
    # Internal: syllable timing generation
    # ------------------------------------------------------------------

    def _to_syllable_timings(
        self,
        words: list[str],
        word_spans: list,
        ratio: float,
        language: str,
        first_flags: list[bool] | None = None,
    ) -> tuple[list[SyllableTiming], AlignmentStats]:
        """Convert word spans to syllable-level timings."""
        match_count = min(len(words), len(word_spans))
        stats = AlignmentStats(total_words=match_count)
        all_timings: list[SyllableTiming] = []
        is_first_overall = True

        for i in range(match_count):
            word = words[i]
            spans = word_spans[i]
            if not spans:
                is_first_overall = False
                continue

            ws = spans[0].start * ratio
            wend = spans[-1].end * ratio
            if wend <= ws:
                wend = ws + 0.05

            # Determine prefix (space/newline).
            if is_first_overall:
                prefix = ""
            elif first_flags and i < len(first_flags) and first_flags[i]:
                prefix = "\n"
            else:
                prefix = " "

            # Split word into syllables.
            parts = self._syllabifier._split_word(word, language)
            if not parts:
                is_first_overall = False
                continue

            duration = wend - ws
            if len(parts) == 1:
                all_timings.append(SyllableTiming(
                    syllable=prefix + parts[0], start=ws, end=wend,
                ))
            else:
                cl = [max(len(p.strip()), 1) for p in parts]
                tc = sum(cl)
                cur = ws
                for pi, part in enumerate(parts):
                    frac = cl[pi] / tc
                    send = cur + duration * frac
                    d = (prefix + part) if pi == 0 else part
                    all_timings.append(SyllableTiming(
                        syllable=d,
                        start=round(cur, 3),
                        end=round(send, 3),
                    ))
                    cur = send
            stats.proportional_fallback += 1
            is_first_overall = False

        return all_timings, stats
