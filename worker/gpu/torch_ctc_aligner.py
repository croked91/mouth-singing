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
        pre_trim_enabled: bool = True,
        pre_trim_threshold: float = 0.7,
        pre_trim_min_speech_ms: int = 300,
        pre_trim_lead_in_ms: int = 100,
        line_start_rms_adjust: bool = True,
        word_end_drift_adjust: bool = True,
        word_end_sustain_extend: bool = True,
    ) -> None:
        self._device = device
        self._cache_dir = model_cache_dir
        self._model = None
        self._bundle = None
        self._dictionary: dict[str, int] = {}
        self._syllabifier = Syllabifier()
        # Silero VAD pre-trim config: before forced alignment, skip any
        # intro ad-libs / inhales / low-confidence noise that would cause
        # MMS to anchor the first word too early. Silero with a high
        # threshold only keeps confident speech onsets.
        self._pre_trim_enabled = pre_trim_enabled
        self._pre_trim_threshold = pre_trim_threshold
        self._pre_trim_min_speech_ms = pre_trim_min_speech_ms
        self._pre_trim_lead_in_ms = pre_trim_lead_in_ms
        self._silero_model = None
        self._silero_get_ts = None
        # Per-line RMS-dip adjustment: every first-in-line word is a
        # natural transition where MMS can accidentally anchor the word
        # to a preceding ad-lib/backing-leakage. Search the natural
        # window [prev_word_end, this_word_end] for a sandwich'ed RMS
        # dip (local minimum with louder peaks on both sides) — that
        # dip is the true gap before the real word onset.
        self._line_start_rms_adjust = line_start_rms_adjust
        # Word-end drift: MMS can extend the last phoneme's emission span
        # into silence/instrumental. Structural filter (duration outlier)
        # identifies candidates; RMS validation cuts only when audio is
        # truly silent, preserving legitimate sustained vocal notes.
        self._word_end_drift_adjust = word_end_drift_adjust
        # Word-end sustain extend: MMS emission fires once per phoneme at
        # attack; a sustained final vowel (common at line-end) gets its
        # word closed at the attack frame. Forward RMS walk from orig_end
        # extends the word to the natural silence boundary, capped by the
        # next word's onset.
        self._word_end_sustain_extend = word_end_sustain_extend
        logger.info(
            "torch_ctc_aligner_created",
            device=device,
            pre_trim_enabled=pre_trim_enabled,
            line_start_rms_adjust=line_start_rms_adjust,
            word_end_drift_adjust=word_end_drift_adjust,
            word_end_sustain_extend=word_end_sustain_extend,
        )

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
            _HF_MODEL_ID,
            torch_dtype=torch.float16,
            **cache_kwargs,
        )
        self._model.to(self._device).eval()

        # Build dictionary from processor vocab.
        # Vocab: <blank>=0, <pad>=1, </s>=2, <unk>=3, a=4, ..., x=30
        processor = Wav2Vec2Processor.from_pretrained(
            _HF_MODEL_ID,
            **cache_kwargs,
        )
        vocab = processor.tokenizer.get_vocab()
        # Keep only single-char alphabetic tokens + apostrophe.
        self._dictionary = {
            k: v for k, v in vocab.items() if len(k) == 1 and (k.isalpha() or k == "'")
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
        """Align lyrics to vocals using GPU-accelerated CTC (full-track).

        Raises:
            ValueError: If lyrics_text is empty.
            RuntimeError: If alignment fails.
        """
        if not lyrics_text or not lyrics_text.strip():
            raise ValueError("lyrics_text is empty")

        self._ensure_model()

        logger.info("ctc_alignment_starting", language=language, device=self._device)
        t0 = time.monotonic()

        waveform = self._load_audio(vocals_path)

        # Silero VAD pre-trim: skip intro noise before the first confident
        # speech onset. Keeps timings stable across the rest of the track
        # but prevents MMS from anchoring the first word to an ad-lib.
        trim_offset = 0.0
        if self._pre_trim_enabled:
            trim_offset = self._silero_trim_start(waveform)
            if trim_offset > 0.0:
                trim_samples = int(trim_offset * _SAMPLE_RATE)
                waveform = waveform[:, trim_samples:]
                logger.info(
                    "ctc_alignment_pre_trim",
                    trim_offset_sec=round(trim_offset, 3),
                    threshold=self._pre_trim_threshold,
                )

        emission, ratio = self._forward_pass(waveform)

        words, transcript, first_flags = self._tokenize_lyrics(lyrics_text, language)
        if not transcript:
            raise RuntimeError("No valid tokens after text preprocessing")

        word_spans = self._align_tokens(emission, transcript)

        # Per-line RMS-dip adjustment (before syllable timing generation).
        line_adjustments = {}
        if self._line_start_rms_adjust:
            line_adjustments = self._compute_line_start_adjustments(
                words, word_spans, ratio, first_flags, waveform,
            )
            if line_adjustments:
                logger.info(
                    "ctc_line_start_adjusted",
                    count=len(line_adjustments),
                    adjustments=[
                        {"word_idx": i, "orig_start_sec": round(v[0], 3),
                         "new_start_sec": round(v[1], 3)}
                        for i, v in list(line_adjustments.items())[:5]
                    ],
                )

        end_adjustments = {}
        if self._word_end_drift_adjust:
            end_adjustments = self._compute_word_end_adjustments(
                words, word_spans, ratio, waveform, time_offset=trim_offset,
            )

        end_extensions = {}
        if self._word_end_sustain_extend:
            end_extensions = self._compute_word_end_extensions(
                words, word_spans, ratio, waveform,
                time_offset=trim_offset,
                end_adjustments=end_adjustments,
            )

        # Drift trims take priority over sustain extensions — a word with
        # a drift adjustment is excluded from extensions by construction,
        # so this merge just collects both independent sets.
        combined_end_adjustments = dict(end_extensions)
        combined_end_adjustments.update(end_adjustments)

        timings, stats = self._to_syllable_timings(
            words,
            word_spans,
            ratio,
            language,
            first_flags,
            time_offset=trim_offset,
            line_adjustments=line_adjustments,
            end_adjustments=combined_end_adjustments,
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
        self._dictionary = {}
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        logger.info("torch_ctc_cleanup_done")

    # ------------------------------------------------------------------
    # Internal: forward pass & token alignment
    # ------------------------------------------------------------------

    def _forward_pass(self, waveform: torch.Tensor) -> tuple[torch.Tensor, float]:
        """Run model forward pass and return (emission, ratio).

        ratio = seconds per emission frame.
        """
        with torch.inference_mode():
            output = self._model(waveform.to(device=self._device, dtype=torch.float16))
            emission = torch.log_softmax(output.logits.float(), dim=-1)

        n_frames = emission.size(1)
        ratio = waveform.size(1) / _SAMPLE_RATE / n_frames
        return emission, ratio

    def _align_tokens(
        self,
        emission: torch.Tensor,
        transcript: list[list[str]],
    ) -> list:
        """Run forced alignment on emission and return per-word span lists."""
        tokenized = [
            self._dictionary[c]
            for word in transcript
            for c in word
            if c in self._dictionary and self._dictionary[c] != 0
        ]
        if not tokenized:
            raise RuntimeError("All tokens mapped to blank")

        targets = torch.tensor([tokenized], dtype=torch.int64).to(emission.device)
        aligned_tokens, scores = torchaudio.functional.forced_align(
            emission,
            targets,
            blank=0,
        )

        token_spans = torchaudio.functional.merge_tokens(
            aligned_tokens[0],
            scores[0],
        )

        word_lengths = [len(word) for word in transcript]
        return self._unflatten(token_spans, word_lengths)

    # ------------------------------------------------------------------
    # Internal: Silero VAD pre-trim
    # ------------------------------------------------------------------

    def _ensure_silero(self) -> None:
        """Lazy-load Silero VAD model via torch.hub."""
        if self._silero_model is not None:
            return
        model, utils = torch.hub.load(
            "snakers4/silero-vad", "silero_vad", trust_repo=True,
        )
        self._silero_model = model
        self._silero_get_ts = utils[0]

    def _silero_trim_start(self, waveform: torch.Tensor) -> float:
        """Return start timestamp (sec) of first confident speech segment,
        refined backwards to the true word onset via RMS back-tracking.

        Waveform: (1, samples) tensor at 16kHz mono.
        Returns 0.0 if no confident speech found.
        """
        self._ensure_silero()
        audio = waveform.squeeze(0).cpu()
        ts = self._silero_get_ts(
            audio,
            self._silero_model,
            threshold=self._pre_trim_threshold,
            sampling_rate=_SAMPLE_RATE,
            min_speech_duration_ms=self._pre_trim_min_speech_ms,
            min_silence_duration_ms=500,
            speech_pad_ms=50,
        )
        if not ts:
            return 0.0
        silero_start_samples = int(ts[0]["start"])
        return self._refine_silero_onset(audio.numpy(), silero_start_samples)

    def _refine_silero_onset(
        self,
        audio_np,
        silero_start_samples: int,
    ) -> float:
        """Refine Silero-detected speech start via RMS back-tracking.

        Silero with threshold=0.7 fires after vocal formants have ramped
        up — the true attack of the word is 100-400ms earlier. Walk back
        through the RMS envelope until we cross a silence floor defined
        relative to the voiced level inside the detected segment (so the
        threshold adapts to each track's loudness).
        """
        import math

        import numpy as np

        frame_len = int(0.02 * _SAMPLE_RATE)  # 20ms frames
        look_after = int(0.5 * _SAMPLE_RATE)  # 500ms after silero_start
        end_idx = min(silero_start_samples + look_after, len(audio_np))
        if end_idx < frame_len:
            return silero_start_samples / _SAMPLE_RATE

        n_frames = end_idx // frame_len
        trimmed = audio_np[: n_frames * frame_len].reshape(n_frames, frame_len)
        rms = np.sqrt((trimmed ** 2).mean(axis=1))

        silero_frame = silero_start_samples // frame_len
        voiced_frames = rms[silero_frame:]
        if voiced_frames.size == 0 or float(voiced_frames.max()) < 1e-5:
            return silero_start_samples / _SAMPLE_RATE

        voiced_level = float(np.median(voiced_frames))
        if voiced_level < 1e-6:
            return silero_start_samples / _SAMPLE_RATE

        # 20 dB below voiced level = standard SNR threshold for silence.
        silence_floor = voiced_level / 10.0

        silero_start_sec = silero_start_samples / _SAMPLE_RATE
        refined_sec = silero_start_sec  # fallback
        required_silent = 2
        silent_run = 0
        for f in range(silero_frame - 1, -1, -1):
            if rms[f] < silence_floor:
                silent_run += 1
                if silent_run >= required_silent:
                    onset_frame = f + silent_run
                    refined_sec = (onset_frame * frame_len) / _SAMPLE_RATE
                    break
            else:
                silent_run = 0

        # Guard: if backtrack somehow exceeds 1s, distrust and fall back.
        if silero_start_sec - refined_sec > 1.0:
            refined_sec = silero_start_sec

        def _to_db(x: float) -> float:
            return 20.0 * math.log10(x + 1e-10)

        logger.info(
            "silero_onset_refine",
            silero_start_sec=round(silero_start_sec, 3),
            refined_onset_sec=round(refined_sec, 3),
            backtrack_ms=round((silero_start_sec - refined_sec) * 1000, 1),
            voiced_level_db=round(_to_db(voiced_level), 2),
            silence_floor_db=round(_to_db(silence_floor), 2),
        )
        return refined_sec

    # ------------------------------------------------------------------
    # Internal: per-line adjustment using CTC phoneme durations
    # ------------------------------------------------------------------

    def _compute_line_start_adjustments(
        self,
        words: list[str],
        word_spans: list,
        ratio: float,
        first_flags: list[bool],
        waveform: torch.Tensor,
    ) -> dict[int, tuple[float, float]]:
        """Detect line-start words whose first phoneme was anchored into
        preceding silence/backing leakage and trim to the real attack.

        Step 1 (structural filter): gap_0 = spans[1].start - spans[0].end
        > 2 × median_gap flags a candidate outlier.

        Step 2 (forward RMS walk): walk [spans[0].start, spans[1].start]
        looking for the first voiced frame that sits AFTER a drift-sized
        silent run (≥ 2 × median_gap frames). That frame is the real
        phoneme attack; shift the word's start there. Mirror of the
        forward walk used by _compute_word_end_adjustments.

        If no drift-sized silent run exists, gap_0 is a legitimate
        sustained first phoneme (Э-то, О-на) and no adjustment is made.

        Returns ``{word_idx: (orig_start_sec, new_start_sec)}``.
        """
        import math
        import statistics

        import numpy as np

        # Global reference: median of ALL inter-phoneme gaps in the track,
        # EXCLUDING each word's gap_0 (which may be inflated and bias
        # the median). For a word with n spans we contribute gaps 1..n-2.
        global_gaps: list[float] = []
        for spans in word_spans:
            for j in range(1, len(spans) - 1):
                global_gaps.append(float(spans[j + 1].start - spans[j].end))
        global_median_gap = (
            statistics.median(global_gaps) if global_gaps else None
        )

        audio_np = waveform.squeeze(0).cpu().numpy()
        frame_len_samples = int(0.02 * _SAMPLE_RATE)  # 20ms

        adjustments: dict[int, tuple[float, float]] = {}
        debug_entries: list[dict] = []
        considered: list[dict] = []
        outlier_factor = 2.0

        for i in range(min(len(words), len(word_spans))):
            spans = word_spans[i]
            if not spans:
                continue
            is_line_start = first_flags[i] if i < len(first_flags) else False
            # word_idx=0 is always treated as a line-start — if Silero
            # pre-trim left residual silence before the first phoneme,
            # its gap_0 will be an outlier and get trimmed like any other.
            if i != 0 and not is_line_start:
                continue
            if len(spans) < 2 or global_median_gap is None:
                continue

            gap0 = float(spans[1].start - spans[0].end)
            ref = max(global_median_gap, 1.0)

            considered.append({
                "word_idx": i,
                "word": words[i][:20],
                "n_spans": len(spans),
                "gap0_frames": round(gap0, 1),
                "median_gap_frames": round(global_median_gap, 1),
                "ratio": round(gap0 / ref, 2),
                "span0_start": int(spans[0].start),
                "span0_end": int(spans[0].end),
                "span1_start": int(spans[1].start),
            })

            if gap0 <= outlier_factor * global_median_gap:
                continue

            orig_start_sec = spans[0].start * ratio

            # Voiced reference: peak RMS over
            # [spans[0].start, next_word_start - ratio]. This superset
            # range captures the true peak wherever it lives:
            #   * span0 area if MMS placed it correctly on loud vocal.
            #   * post-spans[-1].end sustained vocal that lives beyond
            #     MMS's last emission frame (important for 2-phoneme
            #     words like «Я» where spans[-1].end is ~20 ms past
            #     span1.start). Silence frames appended don't lower the
            #     peak because we take max.
            word_start_sample = max(
                0, int(spans[0].start * ratio * _SAMPLE_RATE),
            )
            if i + 1 < len(word_spans) and word_spans[i + 1]:
                next_word_start_sec = word_spans[i + 1][0].start * ratio
                voiced_end_sec = next_word_start_sec - ratio
            else:
                voiced_end_sec = len(audio_np) / _SAMPLE_RATE
            word_end_sample = min(
                len(audio_np),
                int(voiced_end_sec * _SAMPLE_RATE),
            )
            word_seg = audio_np[word_start_sample:word_end_sample]
            word_n = len(word_seg) // frame_len_samples
            if word_n == 0:
                continue
            word_frames = word_seg[: word_n * frame_len_samples].reshape(
                word_n, frame_len_samples,
            )
            word_rms = np.sqrt((word_frames ** 2).mean(axis=1))
            voiced_level = float(word_rms.max())
            if voiced_level < 1e-6:
                continue
            # Single threshold at -14 dB (voiced_level/5): anything below
            # this is NOT main vocal — be it true silence, backing/UVR
            # leakage, inhale/breath, or reverb tail. Real main vocal sits
            # within ~14 dB of the word's peak. A stricter floor than the
            # -20 dB used in other places is warranted here because
            # line-start needs to distinguish "main vocal ONSET" from any
            # pre-attack artifact (continuous low-level content, not just
            # audible silence). Sustained first phonemes keep RMS close
            # to peak, so they stay above -14 dB and drift_seen never
            # triggers → no false trim.
            attack_floor = voiced_level / 5.0

            # Forward RMS walk [spans[0].start, spans[1].start]: find the
            # first frame at main-vocal level AFTER a "not-main-vocal"
            # run (≥ 2 × median_gap frames below attack_floor). That
            # frame is the real phoneme attack. If attack_floor holds
            # throughout, gap_0 is a sustained first phoneme (Э-то,
            # О-на) — leave orig_start.
            ss = max(0, int(spans[0].start * ratio * _SAMPLE_RATE))
            se = min(
                len(audio_np),
                int(spans[1].start * ratio * _SAMPLE_RATE),
            )
            scan_seg = audio_np[ss:se]
            scan_n = len(scan_seg) // frame_len_samples
            if scan_n < 2:
                continue
            scan_frames = scan_seg[: scan_n * frame_len_samples].reshape(
                scan_n, frame_len_samples,
            )
            scan_rms = np.sqrt((scan_frames ** 2).mean(axis=1))

            silent_run_threshold = max(2, int(round(outlier_factor * ref)))
            silent_run = 0
            drift_seen = False
            new_start_frame_in_scan = None
            for idx in range(scan_n):
                above_attack = scan_rms[idx] >= attack_floor
                if drift_seen:
                    if above_attack:
                        new_start_frame_in_scan = idx
                        break
                elif not above_attack:
                    silent_run += 1
                    if silent_run >= silent_run_threshold:
                        drift_seen = True
                else:
                    silent_run = 0

            if new_start_frame_in_scan is None:
                # Walk failed: either fully voiced throughout (sustained
                # first phoneme — Э-то, О-на, Знаешь) or uniform soft
                # pre-attack content with no contrast (rap backing/
                # leakage at ~peak level). Only fall back to span1.start
                # for UNAMBIGUOUS misplacement: ratio ≥ 7 × median_gap.
                # Empirically on test tracks, sustained first vowels sit
                # in the 4-7× band while misplacement cases cluster at
                # 8-14×. Using 7× as the gate keeps sustained cases
                # untouched and fires only when the span0-span1 gap is
                # an order of magnitude beyond typical inter-phoneme
                # spacing — impossible to explain as legitimate phoneme
                # duration.
                extreme_factor = 7.0
                if gap0 / ref >= extreme_factor:
                    new_start_sec = spans[1].start * ratio
                    if new_start_sec > orig_start_sec + ratio:
                        adjustments[i] = (orig_start_sec, new_start_sec)
                        debug_entries.append({
                            "word_idx": i,
                            "word": words[i][:30],
                            "gap0_frames": round(gap0, 1),
                            "median_gap_frames": round(global_median_gap, 1),
                            "ratio_to_ref": round(gap0 / ref, 2),
                            "orig_start_sec": round(orig_start_sec, 3),
                            "new_start_sec": round(new_start_sec, 3),
                            "shift_sec": round(new_start_sec - orig_start_sec, 3),
                            "voiced_level_db": round(
                                20.0 * math.log10(voiced_level + 1e-10), 2,
                            ),
                            "applied": True,
                            "fallback": "span1_start",
                        })
                        continue

                # Not extreme or fallback degenerate — leave orig_start.
                debug_entries.append({
                    "word_idx": i,
                    "word": words[i][:30],
                    "gap0_frames": round(gap0, 1),
                    "median_gap_frames": round(global_median_gap, 1),
                    "ratio_to_ref": round(gap0 / ref, 2),
                    "orig_start_sec": round(orig_start_sec, 3),
                    "new_start_sec": round(orig_start_sec, 3),
                    "shift_sec": 0.0,
                    "voiced_level_db": round(
                        20.0 * math.log10(voiced_level + 1e-10), 2,
                    ),
                    "applied": False,
                })
                continue

            new_start_sec = (
                ss + new_start_frame_in_scan * frame_len_samples
            ) / _SAMPLE_RATE  # pre-offset, per line-start convention

            if new_start_sec <= orig_start_sec + ratio:
                continue

            adjustments[i] = (orig_start_sec, new_start_sec)
            debug_entries.append({
                "word_idx": i,
                "word": words[i][:30],
                "gap0_frames": round(gap0, 1),
                "median_gap_frames": round(global_median_gap, 1),
                "ratio_to_ref": round(gap0 / ref, 2),
                "orig_start_sec": round(orig_start_sec, 3),
                "new_start_sec": round(new_start_sec, 3),
                "shift_sec": round(new_start_sec - orig_start_sec, 3),
                "voiced_level_db": round(
                    20.0 * math.log10(voiced_level + 1e-10), 2,
                ),
                "applied": True,
            })

        applied_count = sum(1 for e in debug_entries if e.get("applied"))
        logger.info(
            "ctc_first_phoneme_trim",
            applied_count=applied_count,
            outlier_count=len(debug_entries),
            considered_count=len(considered),
            global_median_gap_frames=(
                round(global_median_gap, 1)
                if global_median_gap is not None else None
            ),
            global_gaps_n=len(global_gaps),
            outliers=debug_entries[:15],
            considered=considered[:20],
        )
        return adjustments

    # ------------------------------------------------------------------
    # Internal: per-word end adjustment (end drift)
    # ------------------------------------------------------------------

    def _compute_word_end_adjustments(
        self,
        words: list[str],
        word_spans: list,
        ratio: float,
        waveform: torch.Tensor,
        time_offset: float,
    ) -> dict[int, tuple[float, float]]:
        """Detect words whose last phoneme was anchored late into silence.

        MMS forced_align gives ~1-frame emission-only spans, so drift does
        NOT manifest as a long last-phoneme span — it manifests as an
        abnormally large gap BEFORE the last phoneme (the forced path
        skipped through silence/instrumental to place ``л`` late).

        Step 1 (structural filter, mirror of line-start gap_0):
            last_gap = spans[-1].start - spans[-2].end
        Outlier if ``last_gap > 2 × median_gap`` where ``median_gap`` is
        the median of intra-word inter-phoneme gaps (same baseline the
        line-start algorithm uses).

        Step 2 (forward RMS walk): the drift region is
        [spans[-2].end, spans[-1].end]. Walk forward from its start,
        tracking the last voiced frame, until a silent run of
        ``outlier_factor × median_gap`` frames (the same structural
        threshold used by the outlier filter) is seen — that silent run
        is the drift gap, and the trailing edge of the last voiced frame
        before it is the real word end. Walking forward (not backward)
        avoids stopping on the late MMS emission burst at ``orig_end``.
        If no drift-sized silent run is found, the trim is a no-op.

        Returns ``{word_idx: (orig_end_sec, new_end_sec)}`` — absolute sec.
        """
        import math
        import statistics

        import numpy as np

        # Global baseline: median of intra-word inter-phoneme gaps,
        # excluding gap_0 and gap_last (which may be inflated themselves).
        global_gaps: list[float] = []
        for spans in word_spans:
            for j in range(1, len(spans) - 2):
                global_gaps.append(float(spans[j + 1].start - spans[j].end))
        median_gap = statistics.median(global_gaps) if global_gaps else None
        if median_gap is None:
            return {}
        ref_gap = max(median_gap, 1.0)

        outlier_factor = 2.0
        adjustments: dict[int, tuple[float, float]] = {}
        debug_adjusted: list[dict] = []
        considered: list[dict] = []

        audio_np = waveform.squeeze(0).cpu().numpy()
        frame_len_samples = int(0.02 * _SAMPLE_RATE)  # 20ms

        for i in range(min(len(words), len(word_spans))):
            spans = word_spans[i]
            if len(spans) < 2:
                continue

            last_gap = float(spans[-1].start - spans[-2].end)
            if last_gap <= outlier_factor * ref_gap:
                continue

            prev_end_sec = time_offset + spans[-2].end * ratio
            orig_end_sec = time_offset + spans[-1].end * ratio
            considered.append({
                "word_idx": i,
                "word": words[i][:20],
                "last_gap_frames": round(last_gap, 1),
                "median_gap_frames": round(median_gap, 1),
                "ratio": round(last_gap / ref_gap, 2),
                "prev_end_sec": round(prev_end_sec, 3),
                "orig_end_sec": round(orig_end_sec, 3),
            })

            # Voiced reference: peak RMS over the word's aligned region
            # [spans[0].start, spans[-1].end] — known-voiced by construction.
            word_start_sec = time_offset + spans[0].start * ratio
            ws = max(0, int((word_start_sec - time_offset) * _SAMPLE_RATE))
            we = min(
                len(audio_np),
                int((orig_end_sec - time_offset) * _SAMPLE_RATE),
            )
            word_seg = audio_np[ws:we]
            word_n = len(word_seg) // frame_len_samples
            if word_n == 0:
                continue
            word_frames = word_seg[: word_n * frame_len_samples].reshape(
                word_n, frame_len_samples,
            )
            word_rms = np.sqrt((word_frames ** 2).mean(axis=1))
            voiced_level = float(word_rms.max())
            if voiced_level < 1e-6:
                continue
            silence_floor = voiced_level / 10.0  # -20 dB SNR

            # Forward RMS walk across [prev_end_sec, orig_end_sec]. Track
            # last voiced frame; break on the first silent run whose length
            # matches the structural outlier threshold (drift). The late
            # MMS emission burst sitting at orig_end is past this silent
            # run and therefore ignored.
            ss = max(0, int((prev_end_sec - time_offset) * _SAMPLE_RATE))
            se = min(
                len(audio_np),
                int((orig_end_sec - time_offset) * _SAMPLE_RATE),
            )
            scan_seg = audio_np[ss:se]
            scan_n = len(scan_seg) // frame_len_samples
            if scan_n < 2:
                continue
            scan_frames = scan_seg[: scan_n * frame_len_samples].reshape(
                scan_n, frame_len_samples,
            )
            scan_rms = np.sqrt((scan_frames ** 2).mean(axis=1))

            # Drift-silence threshold tied to the same structural rule that
            # flagged this word as an outlier: silent_run >= 2 × median_gap.
            silent_run_threshold = max(
                2, int(round(outlier_factor * ref_gap)),
            )
            silent_run = 0
            last_voiced_idx = -1
            for idx in range(scan_n):
                if scan_rms[idx] >= silence_floor:
                    last_voiced_idx = idx
                    silent_run = 0
                else:
                    silent_run += 1
                    if silent_run >= silent_run_threshold:
                        break

            if last_voiced_idx < 0:
                # No voiced content at all in the window — leave orig_end.
                continue

            new_end_frame_in_scan = last_voiced_idx + 1

            new_end_rel = (
                ss + new_end_frame_in_scan * frame_len_samples
            ) / _SAMPLE_RATE
            new_end_sec = time_offset + new_end_rel

            # Require at least one MMS frame of real trim, and don't cross
            # into the previous phoneme span.
            if new_end_sec >= orig_end_sec - ratio:
                continue
            if new_end_sec <= prev_end_sec + ratio:
                continue

            adjustments[i] = (orig_end_sec, new_end_sec)
            debug_adjusted.append({
                "word_idx": i,
                "word": words[i][:30],
                "last_gap_frames": round(last_gap, 1),
                "median_gap_frames": round(median_gap, 1),
                "ratio_to_ref": round(last_gap / ref_gap, 2),
                "orig_end_sec": round(orig_end_sec, 3),
                "new_end_sec": round(new_end_sec, 3),
                "shift_sec": round(new_end_sec - orig_end_sec, 3),
                "voiced_level_db": round(
                    20.0 * math.log10(voiced_level + 1e-10), 2,
                ),
            })

        logger.info(
            "ctc_word_end_trim",
            adjusted_count=len(debug_adjusted),
            considered_count=len(considered),
            median_gap_frames=round(median_gap, 1),
            global_gaps_n=len(global_gaps),
            adjusted=debug_adjusted[:10],
            considered=considered[:20],
        )
        return adjustments

    # ------------------------------------------------------------------
    # Internal: per-word forward sustain extension
    # ------------------------------------------------------------------

    def _compute_word_end_extensions(
        self,
        words: list[str],
        word_spans: list,
        ratio: float,
        waveform: torch.Tensor,
        time_offset: float,
        end_adjustments: dict[int, tuple[float, float]],
    ) -> dict[int, tuple[float, float]]:
        """Extend word end forward while RMS stays voiced, capped by the
        next word's onset (or track end for the last word).

        MMS ``merge_tokens`` yields emission-only ~1-frame spans, so a
        sustained final vowel (typical at line-end) closes at the
        phoneme's attack frame. This routine walks the RMS envelope
        forward from the original end until a silence floor transition,
        with the next word's start as the natural upper bound.

        Returns ``{word_idx: (orig_end_sec, new_end_sec)}`` (absolute sec).
        """
        import math

        import numpy as np

        audio_np = waveform.squeeze(0).cpu().numpy()
        # Absolute time bound of available audio (waveform is already
        # Silero-trimmed; its index 0 corresponds to ``time_offset``).
        audio_end_sec = time_offset + len(audio_np) / _SAMPLE_RATE
        frame_len_samples = int(0.02 * _SAMPLE_RATE)  # 20ms
        frame_len_sec = frame_len_samples / _SAMPLE_RATE
        required_silent = 2

        extensions: dict[int, tuple[float, float]] = {}
        debug_extended: list[dict] = []
        considered_count = 0

        n = min(len(words), len(word_spans))
        for i in range(n):
            spans = word_spans[i]
            if not spans:
                continue
            # Mutual exclusion: if drift-trim fired, the last phoneme ends
            # early (already trimmed). Extending would undo the trim.
            if i in end_adjustments:
                continue

            orig_end_sec = time_offset + spans[-1].end * ratio

            # Natural forward cap: next word's start (minus 1 alignment
            # frame as margin) or audio end for the last word.
            if i + 1 < n and word_spans[i + 1]:
                next_start_sec = time_offset + word_spans[i + 1][0].start * ratio
                forward_end_sec = next_start_sec - ratio
            else:
                forward_end_sec = audio_end_sec

            if forward_end_sec - orig_end_sec < 2 * frame_len_sec:
                continue

            # Voiced reference: peak RMS across the word's own aligned
            # region (known-voiced by construction).
            word_start_sec = time_offset + spans[0].start * ratio
            ws_sample = max(0, int((word_start_sec - time_offset) * _SAMPLE_RATE))
            we_sample = min(len(audio_np), int((orig_end_sec - time_offset) * _SAMPLE_RATE))
            word_seg = audio_np[ws_sample:we_sample]
            word_frame_n = len(word_seg) // frame_len_samples
            if word_frame_n == 0:
                continue
            word_frames = word_seg[: word_frame_n * frame_len_samples].reshape(
                word_frame_n, frame_len_samples,
            )
            word_rms = np.sqrt((word_frames ** 2).mean(axis=1))
            voiced_level = float(word_rms.max())
            if voiced_level < 1e-6:
                continue
            silence_floor = voiced_level / 10.0  # -20 dB SNR

            # Forward scan segment [orig_end_sec, forward_end_sec].
            fs_sample = max(0, int((orig_end_sec - time_offset) * _SAMPLE_RATE))
            fe_sample = min(len(audio_np), int((forward_end_sec - time_offset) * _SAMPLE_RATE))
            scan_seg = audio_np[fs_sample:fe_sample]
            scan_n = len(scan_seg) // frame_len_samples
            if scan_n < 2:
                continue
            considered_count += 1
            scan_frames = scan_seg[: scan_n * frame_len_samples].reshape(
                scan_n, frame_len_samples,
            )
            scan_rms = np.sqrt((scan_frames ** 2).mean(axis=1))

            last_voiced_idx = -1  # index within scan_rms
            silent_run = 0
            capped_by = "next_word"
            for idx, energy in enumerate(scan_rms):
                if energy >= silence_floor:
                    last_voiced_idx = idx
                    silent_run = 0
                else:
                    silent_run += 1
                    if silent_run >= required_silent:
                        capped_by = "silence"
                        break

            if last_voiced_idx < 0:
                # Not a single voiced frame forward — MMS closed the word
                # correctly at a silence boundary.
                continue

            # End of the last voiced frame (frame boundary in absolute sec).
            new_end_rel = (
                fs_sample + (last_voiced_idx + 1) * frame_len_samples
            ) / _SAMPLE_RATE
            new_end_sec = time_offset + new_end_rel

            if new_end_sec <= orig_end_sec + ratio:
                continue

            extensions[i] = (orig_end_sec, new_end_sec)
            debug_extended.append({
                "word_idx": i,
                "word": words[i][:30],
                "orig_end_sec": round(orig_end_sec, 3),
                "new_end_sec": round(new_end_sec, 3),
                "shift_sec": round(new_end_sec - orig_end_sec, 3),
                "capped_by": capped_by,
                "voiced_level_db": round(
                    20.0 * math.log10(voiced_level + 1e-10), 2,
                ),
            })

        logger.info(
            "ctc_word_end_extend",
            extended_count=len(debug_extended),
            considered_count=considered_count,
            extended=debug_extended[:10],
        )
        return extensions

    # ------------------------------------------------------------------
    # Internal: audio loading
    # ------------------------------------------------------------------

    def _load_audio(self, path: str) -> torch.Tensor:
        """Load audio as 16 kHz mono tensor."""
        import soundfile as sf
        import torchaudio.functional as F

        data, sr = sf.read(path, dtype="float32")
        if data.ndim > 1:
            data = data.mean(axis=1)
        t = torch.from_numpy(data)
        if sr != _SAMPLE_RATE:
            t = F.resample(t, sr, _SAMPLE_RATE)
        return t.unsqueeze(0)  # (1, samples)

    # ------------------------------------------------------------------
    # Internal: text preprocessing
    # ------------------------------------------------------------------

    def _tokenize_lyrics(
        self,
        lyrics_text: str,
        language: str,
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
                    c
                    for c in romanized
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
        time_offset: float = 0.0,
        is_first_overall: bool = True,
        line_adjustments: dict[int, tuple[float, float]] | None = None,
        end_adjustments: dict[int, tuple[float, float]] | None = None,
    ) -> tuple[list[SyllableTiming], AlignmentStats]:
        """Convert word spans to syllable-level timings."""
        match_count = min(len(words), len(word_spans))
        stats = AlignmentStats(total_words=match_count)
        all_timings: list[SyllableTiming] = []
        line_adjustments = line_adjustments or {}
        end_adjustments = end_adjustments or {}

        for i in range(match_count):
            word = words[i]
            spans = word_spans[i]
            if not spans:
                is_first_overall = False
                continue

            if i in line_adjustments:
                # RMS-dip adjusted start (in waveform time, pre-offset)
                ws = time_offset + line_adjustments[i][1]
            else:
                ws = time_offset + spans[0].start * ratio
            if i in end_adjustments:
                # End-drift trimmed end (absolute sec, includes time_offset)
                wend = end_adjustments[i][1]
            else:
                wend = time_offset + spans[-1].end * ratio
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
                all_timings.append(
                    SyllableTiming(
                        syllable=prefix + parts[0],
                        start=round(ws, 3),
                        end=round(wend, 3),
                    )
                )
            else:
                cl = [max(len(p.strip()), 1) for p in parts]
                tc = sum(cl)
                cur = ws
                for pi, part in enumerate(parts):
                    frac = cl[pi] / tc
                    send = cur + duration * frac
                    d = (prefix + part) if pi == 0 else part
                    all_timings.append(
                        SyllableTiming(
                            syllable=d,
                            start=round(cur, 3),
                            end=round(send, 3),
                        )
                    )
                    cur = send
            stats.proportional_fallback += 1
            is_first_overall = False

        return all_timings, stats
