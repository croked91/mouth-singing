"""Hybrid CTC forced alignment for syllable-level timing.

Implements word-level CTC on full emissions → per-word char-level CTC
on emissions slices → pyphen syllable assembly.

Ported from m3_test/variant_ctc/experiment_hybrid.py.

CRITICAL:
  - generate_emissions() is called ONCE per track. Repeated calls on
    different audio slices cause heap corruption in ONNX Runtime.
  - Before char-level alignment, n_frames > n_targets MUST be checked.
    Violation raises C++ std::runtime_error — uncatchable in Python.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np
import structlog

from karaoke_shared.models.track import SyllableTiming

logger = structlog.get_logger(__name__)

# Minimum emission frames for char-level CTC. 10 frames ≈ 200ms at stride=20ms.
MIN_FRAMES_FOR_CHAR = 10

# ISO 639-3 codes for ctc_forced_aligner.preprocess_text
_LANG_ISO3 = {"ru": "rus", "en": "eng"}


@dataclass
class AlignmentStats:
    """Alignment quality statistics."""
    total_words: int = 0
    char_level_used: int = 0
    proportional_fallback: int = 0


class CTCAligner:
    """Hybrid CTC alignment: word-level boundaries + per-word char-level.

    Args:
        syllabifier: Syllabifier instance for pyphen word splitting.
        model_cache_dir: Directory for ONNX model cache (optional).
        min_frames_for_char: Minimum frames for char-level CTC (default 10).
    """

    def __init__(
        self,
        syllabifier,
        model_cache_dir: str | None = None,
        min_frames_for_char: int = MIN_FRAMES_FOR_CHAR,
    ) -> None:
        from ctc_forced_aligner import AlignmentSingleton

        self._min_frames = min_frames_for_char
        self._syllabifier = syllabifier

        aligner = AlignmentSingleton()
        self._model = aligner.alignment_model
        self._tokenizer = aligner.alignment_tokenizer

        logger.info("ctc_aligner_loaded")

    def align(
        self,
        vocals_path: str,
        lyrics_text: str,
        language: str,
    ) -> tuple[list[SyllableTiming], AlignmentStats]:
        """Align lyrics to audio and return syllable timings.

        Args:
            vocals_path: Path to vocal WAV (16kHz preferred).
            lyrics_text: Full lyrics with \\n line breaks.
            language: Two-letter code ('ru', 'en').

        Returns:
            Tuple of (syllable_timings, stats).

        Raises:
            ValueError: If lyrics_text is empty.
            RuntimeError: If audio loading or emission generation fails.
        """
        if not lyrics_text or not lyrics_text.strip():
            raise ValueError("lyrics_text is empty")

        from ctc_forced_aligner import load_audio

        audio_waveform = load_audio(vocals_path, ret_type="np")

        # Step 1: compute emissions ONCE for the full track
        emissions, stride_ms = self._compute_emissions(audio_waveform)

        # Step 2: word-level alignment on full emissions
        lyrics_flat = lyrics_text.replace("\n", " ").strip()
        word_timestamps = self._run_word_alignment(
            emissions, stride_ms, lyrics_flat, language
        )

        # Step 3: per-word char-level → syllable assembly
        return self._build_syllable_timings(
            word_timestamps, lyrics_text, emissions, stride_ms, language
        )

    def _compute_emissions(self, waveform: np.ndarray) -> tuple[np.ndarray, int]:
        """Generate emissions ONCE for the full waveform.

        Returns:
            (emissions array (T, V), stride_ms).
        """
        from ctc_forced_aligner import generate_emissions

        emissions, stride_ms = generate_emissions(
            self._model, waveform, batch_size=16
        )
        logger.debug(
            "emissions_computed",
            shape=emissions.shape,
            stride_ms=stride_ms,
        )
        return emissions, stride_ms

    def _run_word_alignment(
        self,
        emissions: np.ndarray,
        stride_ms: int,
        lyrics_flat: str,
        language: str,
    ) -> list[dict]:
        """Word-level CTC on full emissions.

        Returns:
            List of {"text": str, "start": float, "end": float}.
        """
        from ctc_forced_aligner import (
            get_alignments,
            get_spans,
            postprocess_results,
            preprocess_text,
        )

        lang_iso3, romanize = self._lang_flags(language)

        tokens_starred, text_starred = preprocess_text(
            lyrics_flat,
            romanize=romanize,
            language=lang_iso3,
            split_size="word",
        )

        segments, scores, blank_token = get_alignments(
            emissions, tokens_starred, self._tokenizer
        )
        spans = get_spans(tokens_starred, segments, blank_token)
        word_timestamps = postprocess_results(text_starred, spans, stride_ms, scores)

        logger.debug("word_alignment_done", count=len(word_timestamps))
        return word_timestamps

    def _run_char_alignment_on_slice(
        self,
        word_emissions: np.ndarray,
        stride_ms: int,
        word_text: str,
        language: str,
    ) -> list[dict] | None:
        """Char-level CTC on an emissions slice for one word.

        Returns timings RELATIVE to the slice start, or None on failure.
        MUST check n_frames > n_targets before calling get_alignments.
        """
        from ctc_forced_aligner import (
            get_alignments,
            get_spans,
            postprocess_results,
            preprocess_text,
        )

        lang_iso3, romanize = self._lang_flags(language)

        try:
            tokens_starred, text_starred = preprocess_text(
                word_text,
                romanize=romanize,
                language=lang_iso3,
                split_size="char",
            )
        except Exception:
            return None

        # CTC constraint: frames must exceed target count.
        # The tokenizer splits joined tokens on spaces, so romanized
        # multi-char tokens inflate the real target length.
        real_target_tokens = " ".join(tokens_starred).split(" ")
        n_targets = len(real_target_tokens)
        n_frames = word_emissions.shape[0]

        if n_frames <= n_targets:
            return None

        try:
            segments, scores, blank_token = get_alignments(
                word_emissions, tokens_starred, self._tokenizer
            )
            spans = get_spans(tokens_starred, segments, blank_token)
            char_timestamps = postprocess_results(
                text_starred, spans, stride_ms, scores
            )
        except Exception:
            return None

        content_chars = [e for e in char_timestamps if e["text"].strip()]
        return content_chars if content_chars else None

    def _build_syllable_timings(
        self,
        word_timestamps: list[dict],
        lyrics_text: str,
        emissions: np.ndarray,
        stride_ms: int,
        language: str,
    ) -> tuple[list[SyllableTiming], AlignmentStats]:
        """Main loop: word → char → syllable assembly."""
        # Build flat list of (word_text, is_first_in_line) from lyrics
        lyrics_words: list[tuple[str, bool]] = []
        for line in lyrics_text.splitlines():
            words = line.split()
            if not words:
                continue
            for idx, word in enumerate(words):
                lyrics_words.append((word, idx == 0))

        ctc_count = len(word_timestamps)
        lyrics_count = len(lyrics_words)
        match_count = min(ctc_count, lyrics_count)

        if ctc_count != lyrics_count:
            logger.warning(
                "word_count_mismatch",
                ctc=ctc_count,
                lyrics=lyrics_count,
                using=match_count,
            )

        total_frames = emissions.shape[0]
        stats = AlignmentStats(total_words=match_count)
        all_timings: list[SyllableTiming] = []
        is_first_overall = True

        for i in range(match_count):
            word_entry = word_timestamps[i]
            lyrics_word, is_first_in_line = lyrics_words[i]

            word_start = word_entry["start"]
            word_end = word_entry["end"]
            if word_end <= word_start:
                word_end = word_start + 0.05

            # Display prefix
            if is_first_overall:
                prefix = ""
            elif is_first_in_line:
                prefix = "\n"
            else:
                prefix = " "

            # Convert to frame indices
            frame_start = self._time_to_frame(word_start, stride_ms)
            frame_end = self._time_to_frame(word_end, stride_ms)
            frame_start = max(0, min(frame_start, total_frames - 1))
            frame_end = max(frame_start + 1, min(frame_end, total_frames))
            num_frames = frame_end - frame_start

            char_timings: list[dict] | None = None

            if num_frames >= self._min_frames:
                word_emissions = emissions[frame_start:frame_end]
                char_timings = self._run_char_alignment_on_slice(
                    word_emissions, stride_ms, lyrics_word, language
                )

            if char_timings is not None:
                syl_timings = self._syllables_from_char_timings(
                    char_timings, lyrics_word, word_start, word_end,
                    language, prefix,
                )
                if syl_timings is not None:
                    all_timings.extend(syl_timings)
                    stats.char_level_used += 1
                else:
                    # Char count mismatch — fallback
                    all_timings.extend(self._proportional_syllables(
                        lyrics_word, word_start, word_end, language, prefix,
                    ))
                    stats.proportional_fallback += 1
            else:
                all_timings.extend(self._proportional_syllables(
                    lyrics_word, word_start, word_end, language, prefix,
                ))
                stats.proportional_fallback += 1

            is_first_overall = False

        logger.info(
            "alignment_complete",
            total_words=stats.total_words,
            char_level=stats.char_level_used,
            fallback=stats.proportional_fallback,
            syllables=len(all_timings),
        )
        return all_timings, stats

    def _proportional_syllables(
        self,
        word_text: str,
        word_start: float,
        word_end: float,
        language: str,
        first_prefix: str,
    ) -> list[SyllableTiming]:
        """Split word into syllables with timing proportional to char count."""
        duration = word_end - word_start
        parts = self._syllabifier._split_word(word_text, language)
        if not parts:
            return []

        if len(parts) == 1:
            return [SyllableTiming(
                syllable=first_prefix + parts[0],
                start=word_start,
                end=word_end,
            )]

        char_lengths = [max(len(p.strip()), 1) for p in parts]
        total_chars = sum(char_lengths)

        timings: list[SyllableTiming] = []
        cursor = word_start

        for i, part in enumerate(parts):
            fraction = char_lengths[i] / total_chars
            syl_end = cursor + duration * fraction
            display = first_prefix + part if i == 0 else part
            timings.append(SyllableTiming(
                syllable=display, start=round(cursor, 3), end=round(syl_end, 3),
            ))
            cursor = syl_end

        return timings

    def _syllables_from_char_timings(
        self,
        char_timings: list[dict],
        word_text: str,
        word_start: float,
        word_end: float,
        language: str,
        first_prefix: str,
    ) -> list[SyllableTiming] | None:
        """Build syllable timings by consuming N chars per pyphen syllable.

        Char timings are relative to the emission slice start (0.0 = word_start).
        """
        parts = self._syllabifier._split_word(word_text, language)
        if not parts:
            return None

        char_cursor = 0
        timings: list[SyllableTiming] = []

        for i, part in enumerate(parts):
            n_chars = max(len(re.sub(r"[^\w]", "", part, flags=re.UNICODE)), 1)

            if char_cursor + n_chars > len(char_timings):
                return None

            consumed = char_timings[char_cursor:char_cursor + n_chars]
            char_cursor += n_chars

            syl_start = min(
                max(consumed[0]["start"] + word_start, word_start), word_end
            )
            syl_end = min(
                max(consumed[-1]["end"] + word_start, syl_start + 0.01), word_end
            )

            display = first_prefix + part if i == 0 else part
            timings.append(SyllableTiming(
                syllable=display,
                start=round(syl_start, 3),
                end=round(syl_end, 3),
            ))

        return timings

    @staticmethod
    def _lang_flags(language: str) -> tuple[str, bool]:
        """Return (iso639_3, romanize) for a two-letter language code."""
        lang_iso3 = _LANG_ISO3.get(language, "eng")
        romanize = language != "en"
        return lang_iso3, romanize

    @staticmethod
    def _time_to_frame(time_sec: float, stride_ms: int) -> int:
        """Convert time in seconds to emission frame index."""
        return int(time_sec * 1000 / stride_ms)
