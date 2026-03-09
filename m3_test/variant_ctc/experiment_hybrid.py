"""Variant CTC experiment (hybrid): word-level boundaries + char-level internals.

Pipeline per track:
  1. Load audio with ctc_forced_aligner.load_audio (numpy, 16kHz mono).
  2. Run generate_emissions ONCE on the full waveform -> (T, V) emissions + stride.
  3. Run word-level CTC alignment on the full emissions -> word timings.
  4. For each aligned word:
       a. Slice the emissions tensor to the word's frame range.
       b. Run char-level get_alignments + get_spans + postprocess_results on
          that slice — no second ONNX inference call needed.
       c. Char timings are relative to the slice; add word.start for absolute.
       d. Assemble syllable timings by consuming N chars per pyphen syllable.
       e. If the word's frame range is too narrow or char alignment fails,
          fall back to proportional split within the word's interval.
  5. Collect all syllable timings, evaluate against reference.

Why slice emissions instead of slicing audio and re-running the model?
  Calling generate_emissions hundreds of times per track (once per word) causes
  heap corruption in ONNX Runtime's internal memory allocator.  Slicing the
  already-computed (T, V) emissions array is free and produces identical results
  because CTC forced alignment is deterministic given fixed emissions.

Results saved to m3_test/variant_ctc/results/results_hybrid.json and
summary_hybrid.txt.  Existing results.json and results_char.json are
never touched.
"""

from __future__ import annotations

import difflib
import json
import math
import re
import sys
from pathlib import Path
from typing import NamedTuple

import numpy

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------
sys.path.insert(0, "/home/croked/karaoke/v2/shared")

from karaoke_shared.utils.syllabifier import Syllabifier  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TEST_DATA_DIR = Path("/home/croked/karaoke/m3_test/test_data")
RESULTS_DIR = Path("/home/croked/karaoke/m3_test/variant_ctc/results")
TRACK_IDS = [1, 2, 3, 4, 5]

# Minimum number of emission frames a word slice must have for char CTC.
# Each ONNX window is ~20ms; 10 frames = ~0.2s.  Below this threshold the
# forced-align Viterbi path has nowhere meaningful to go.
MIN_FRAMES_FOR_CHAR = 10

# ISO 639-3 codes required by ctc_forced_aligner.preprocess_text
_LANG_ISO3 = {"ru": "rus", "en": "eng"}


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class TrackMeta(NamedTuple):
    track_num: int
    artist: str
    title: str
    language: str
    track_id: str
    vocals_path: Path
    lyrics_path: Path
    reference_path: Path


class SyllableTiming(NamedTuple):
    syllable: str
    start: float
    end: float

    def as_dict(self) -> dict:
        return {"syllable": self.syllable, "start": self.start, "end": self.end}


# ---------------------------------------------------------------------------
# Shared CTC helpers
# ---------------------------------------------------------------------------


def _lang_flags(language: str) -> tuple[str, bool]:
    """Return (lang_iso3, romanize) for a two-letter language code.

    Args:
        language: Two-letter code e.g. "ru" or "en".

    Returns:
        Tuple of ISO 639-3 code and whether to romanize Cyrillic to Latin.
    """
    lang_iso3 = _LANG_ISO3.get(language, "eng")
    romanize = language != "en"
    return lang_iso3, romanize


def _time_to_frame(time_sec: float, stride_ms: int) -> int:
    """Convert a time in seconds to an emission frame index.

    Args:
        time_sec: Time in seconds.
        stride_ms: Stride in milliseconds as returned by generate_emissions.

    Returns:
        Integer frame index (floor).
    """
    return int(time_sec * 1000 / stride_ms)


# ---------------------------------------------------------------------------
# Full-track emissions (run once per track)
# ---------------------------------------------------------------------------


def compute_emissions(
    audio_waveform,
    alignment_model,
) -> tuple[numpy.ndarray, int]:
    """Run the ONNX model on the full waveform and return emissions + stride.

    This is the only call to generate_emissions per track.  The resulting
    emissions array is sliced per-word for char-level alignment.

    Args:
        audio_waveform: 1D numpy float32 array (16kHz, from load_audio).
        alignment_model: ONNX InferenceSession.

    Returns:
        Tuple of:
          - emissions: numpy array of shape (T_frames, vocab_size), float32.
          - stride_ms: milliseconds per frame (integer).
    """
    from ctc_forced_aligner import generate_emissions

    print(f"    [Emit] Generating emissions (audio shape: {audio_waveform.shape}) ...")
    emissions, stride_ms = generate_emissions(alignment_model, audio_waveform, batch_size=16)
    print(f"    [Emit] Emissions shape: {emissions.shape}, stride: {stride_ms}ms")
    return emissions, stride_ms


# ---------------------------------------------------------------------------
# Word-level alignment on full-track emissions
# ---------------------------------------------------------------------------


def run_word_alignment(
    emissions: numpy.ndarray,
    stride_ms: int,
    lyrics_flat: str,
    language: str,
    alignment_tokenizer,
) -> list[dict]:
    """Run CTC forced alignment at word level using pre-computed emissions.

    Args:
        emissions: Full-track emissions array (T, V).
        stride_ms: Milliseconds per emission frame.
        lyrics_flat: Full lyrics as a single string (newlines replaced by spaces).
        language: Two-letter language code.
        alignment_tokenizer: Tokenizer paired with the model.

    Returns:
        List of {"text": str, "start": float, "end": float} dicts (seconds).
    """
    from ctc_forced_aligner import (
        get_alignments,
        get_spans,
        postprocess_results,
        preprocess_text,
    )

    lang_iso3, romanize = _lang_flags(language)

    tokens_starred, text_starred = preprocess_text(
        lyrics_flat,
        romanize=romanize,
        language=lang_iso3,
        split_size="word",
    )
    word_count = len([t for t in tokens_starred if t != "<star>"])
    print(f"    [Word] Preprocessing done: {word_count} words")

    segments, scores, blank_token = get_alignments(
        emissions, tokens_starred, alignment_tokenizer
    )
    spans = get_spans(tokens_starred, segments, blank_token)
    word_timestamps = postprocess_results(text_starred, spans, stride_ms, scores)

    print(f"    [Word] Got {len(word_timestamps)} word-level timings.")
    return word_timestamps


# ---------------------------------------------------------------------------
# Char-level alignment on a single word's emissions slice
# ---------------------------------------------------------------------------


def run_char_alignment_on_emissions_slice(
    word_emissions: numpy.ndarray,
    stride_ms: int,
    word_text: str,
    language: str,
    alignment_tokenizer,
) -> list[dict] | None:
    """Run CTC char-level alignment on a sliced emissions window for one word.

    No ONNX model call is made — we reuse the already-computed emissions.
    The returned timings are relative to the slice (i.e., relative to the
    first frame of word_emissions, in seconds from frame 0 of the slice).

    CTC constraint: the number of frames must be >= 2 * n_content_chars.
    If the slice is too narrow, None is returned so the caller can fall back.
    This check must happen before calling get_alignments because a violation
    raises a C++ std::runtime_error that aborts the process (not catchable
    as a Python exception).

    Args:
        word_emissions: Emissions slice for this word, shape (F, vocab_size).
        stride_ms: Milliseconds per frame (same as full-track).
        word_text: Word string to align against.
        language: Two-letter language code.
        alignment_tokenizer: Tokenizer.

    Returns:
        List of content-char dicts {"text", "start", "end"} with times
        relative to the slice start, or None if alignment fails or the
        slice is too short for CTC.
    """
    import re as _re

    from ctc_forced_aligner import (
        get_alignments,
        get_spans,
        postprocess_results,
        preprocess_text,
    )

    lang_iso3, romanize = _lang_flags(language)

    try:
        tokens_starred, text_starred = preprocess_text(
            word_text,
            romanize=romanize,
            language=lang_iso3,
            split_size="char",
        )
    except Exception as exc:
        print(f"      [Char] preprocess_text failed for '{word_text}': {exc}")
        return None

    # The CTC library builds its targets list by splitting the joined token
    # string on spaces: " ".join(tokens_starred).split(" ").  A romanized
    # multi-character token like "ia" (for "я") becomes two entries after
    # that split, so the real target length can exceed len(tokens_starred).
    #
    # CTC requires T >= target_length (frames >= number of target tokens).
    # We use T > target_length as a safe margin (T == target_length leaves
    # zero room for blanks between adjacent identical tokens).
    real_target_tokens = " ".join(tokens_starred).split(" ")
    n_targets = len(real_target_tokens)
    n_frames = word_emissions.shape[0]

    if n_frames <= n_targets:
        return None

    try:
        segments, scores, blank_token = get_alignments(
            word_emissions, tokens_starred, alignment_tokenizer
        )
        spans = get_spans(tokens_starred, segments, blank_token)
        char_timestamps = postprocess_results(text_starred, spans, stride_ms, scores)
    except Exception as exc:
        print(f"      [Char] alignment failed for '{word_text}': {exc}")
        return None

    # Filter to content chars only (no spaces or empty entries).
    content_chars = [e for e in char_timestamps if e["text"].strip()]

    if not content_chars:
        return None

    return content_chars


# ---------------------------------------------------------------------------
# Syllable assembly helpers
# ---------------------------------------------------------------------------


def _proportional_syllables(
    word_text: str,
    word_start: float,
    word_end: float,
    syllabifier: Syllabifier,
    language: str,
    first_prefix: str,
) -> list[SyllableTiming]:
    """Split a word into syllables with timing proportional to char count.

    Used as fallback when char-level CTC alignment cannot produce results.

    Args:
        word_text: The original word string.
        word_start: Absolute start time (seconds).
        word_end: Absolute end time (seconds).
        syllabifier: Syllabifier instance.
        language: Two-letter language code.
        first_prefix: Display prefix for the first syllable ("", "\\n", " ").

    Returns:
        List of SyllableTiming objects.
    """
    duration = word_end - word_start
    parts = syllabifier._split_word(word_text, language)  # noqa: SLF001
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
        timings.append(SyllableTiming(syllable=display, start=cursor, end=syl_end))
        cursor = syl_end

    return timings


def _syllables_from_char_timings(
    char_timings: list[dict],
    word_text: str,
    word_start: float,
    word_end: float,
    syllabifier: Syllabifier,
    language: str,
    first_prefix: str,
) -> list[SyllableTiming] | None:
    """Build syllable timings by consuming N chars per pyphen syllable.

    Char timings are relative to the word's emission slice (i.e., times are
    relative to the first frame of the slice = word_start in the track).
    We add word_start to convert to absolute times and clip to [word_start, word_end].

    Args:
        char_timings: Content-char entries; times are relative to slice start
            (so 0.0 corresponds to word_start in the track).
        word_text: Original word string.
        word_start: Absolute word start (seconds) — added to char times.
        word_end: Absolute word end (seconds) — used as ceiling.
        syllabifier: Syllabifier instance.
        language: Two-letter language code.
        first_prefix: Display prefix for first syllable.

    Returns:
        List of SyllableTiming objects, or None if char count mismatches.
    """
    parts = syllabifier._split_word(word_text, language)  # noqa: SLF001
    if not parts:
        return None

    char_cursor = 0
    timings: list[SyllableTiming] = []

    for i, part in enumerate(parts):
        n_chars = max(len(re.sub(r"[^\w]", "", part, flags=re.UNICODE)), 1)

        if char_cursor + n_chars > len(char_timings):
            # Not enough char entries — caller should fall back.
            return None

        consumed = char_timings[char_cursor : char_cursor + n_chars]
        char_cursor += n_chars

        # Char timings are relative to slice start; add word_start for absolute.
        syl_start = min(max(consumed[0]["start"] + word_start, word_start), word_end)
        syl_end = min(
            max(consumed[-1]["end"] + word_start, syl_start + 0.01), word_end
        )

        display = first_prefix + part if i == 0 else part
        timings.append(SyllableTiming(syllable=display, start=syl_start, end=syl_end))

    return timings


# ---------------------------------------------------------------------------
# Hybrid syllable builder
# ---------------------------------------------------------------------------


def build_hybrid_syllable_timings(
    word_timestamps: list[dict],
    lyrics_text: str,
    emissions: numpy.ndarray,
    stride_ms: int,
    language: str,
    alignment_tokenizer,
    syllabifier: Syllabifier,
) -> tuple[list[SyllableTiming], dict]:
    """Build syllable timings using word-level boundaries + per-word char CTC.

    For each word:
      1. Convert word time to emission frame indices.
      2. If the frame slice is too narrow (< MIN_FRAMES_FOR_CHAR): fallback.
      3. Otherwise: run char-level alignment on the emissions slice.
      4. If char alignment fails or char count mismatches: fallback.
      5. Fallback = proportional split within [word.start, word.end].

    No additional ONNX inference is performed — we slice the emissions array
    computed once for the full track.

    Args:
        word_timestamps: Output of run_word_alignment.
        lyrics_text: Full lyrics with original newlines.
        emissions: Full-track emissions (T, V), float32.
        stride_ms: Milliseconds per emission frame.
        language: Two-letter language code.
        alignment_tokenizer: Tokenizer.
        syllabifier: Syllabifier instance.

    Returns:
        Tuple of (syllable_timings, stats) where stats counts char-level vs
        proportional fallback words.
    """
    # Build flat list of (word_text, is_first_in_line) from lyrics structure.
    lyrics_words: list[tuple[str, bool]] = []
    for line in lyrics_text.splitlines():
        words = line.split()
        if not words:
            continue
        for word_idx, word in enumerate(words):
            lyrics_words.append((word, word_idx == 0))

    ctc_count = len(word_timestamps)
    lyrics_count = len(lyrics_words)
    match_count = min(ctc_count, lyrics_count)

    if ctc_count != lyrics_count:
        print(f"    [Hybrid] Warning: CTC words ({ctc_count}) != "
              f"lyrics words ({lyrics_count}), matching first {match_count}.")

    total_frames = emissions.shape[0]

    stats = {
        "total_words": match_count,
        "char_level_used": 0,
        "proportional_fallback": 0,
        "fallback_reasons": [],
    }

    all_timings: list[SyllableTiming] = []
    is_first_syllable_overall = True

    for i in range(match_count):
        word_entry = word_timestamps[i]
        lyrics_word, is_first_in_line = lyrics_words[i]

        word_start = word_entry["start"]
        word_end = word_entry["end"]

        # Ensure end > start.
        if word_end <= word_start:
            word_end = word_start + 0.05

        # Determine display prefix for the first syllable of this word.
        if is_first_syllable_overall:
            first_prefix = ""
        elif is_first_in_line:
            first_prefix = "\n"
        else:
            first_prefix = " "

        fallback_reason: str | None = None
        char_timings: list[dict] | None = None

        # Convert word time boundaries to emission frame indices.
        frame_start = _time_to_frame(word_start, stride_ms)
        frame_end = _time_to_frame(word_end, stride_ms)

        # Clamp to valid range.
        frame_start = max(0, min(frame_start, total_frames - 1))
        frame_end = max(frame_start + 1, min(frame_end, total_frames))

        num_frames = frame_end - frame_start

        if num_frames < MIN_FRAMES_FOR_CHAR:
            fallback_reason = (
                f"too few frames ({num_frames} < {MIN_FRAMES_FOR_CHAR})"
            )
        else:
            word_emissions = emissions[frame_start:frame_end]
            char_timings = run_char_alignment_on_emissions_slice(
                word_emissions,
                stride_ms,
                lyrics_word,
                language,
                alignment_tokenizer,
            )
            if char_timings is None:
                fallback_reason = "char alignment returned no content chars"

        if char_timings is not None:
            syl_timings = _syllables_from_char_timings(
                char_timings,
                lyrics_word,
                word_start,
                word_end,
                syllabifier,
                language,
                first_prefix,
            )
            if syl_timings is None:
                fallback_reason = "char count mismatch after alignment"
            else:
                all_timings.extend(syl_timings)
                stats["char_level_used"] += 1

        if fallback_reason is not None:
            syl_timings = _proportional_syllables(
                lyrics_word,
                word_start,
                word_end,
                syllabifier,
                language,
                first_prefix,
            )
            all_timings.extend(syl_timings)
            stats["proportional_fallback"] += 1
            stats["fallback_reasons"].append(
                {"word": lyrics_word, "reason": fallback_reason}
            )

        is_first_syllable_overall = False

    return all_timings, stats


# ---------------------------------------------------------------------------
# Evaluation (identical logic to experiment.py / experiment_char.py)
# ---------------------------------------------------------------------------


def _strip_prefix(syllable: str) -> str:
    """Remove space or newline prefix used as word/line boundary marker."""
    return syllable.lstrip(" \n")


def evaluate_timings(
    predicted: list[SyllableTiming],
    reference: list[dict],
    label: str,
) -> dict:
    """Compare predicted syllable timings to reference timings.

    Syllables are aligned by cleaned text using SequenceMatcher, which
    handles dropped or extra syllables without shifting all subsequent rows.

    Metrics:
    - MAE: mean absolute error of start times for matched syllable pairs.
    - Hit rate: fraction of matched syllables with |delta_start| < 0.1s.

    Args:
        predicted: SyllableTiming list from build_hybrid_syllable_timings.
        reference: List of {"syllable": str, "start": float, ...} dicts.
        label: Short name for display (e.g. "hybrid").

    Returns:
        Dict with "mae", "hit_rate_01s", "matched_count", "total_ref",
        and "first_20" keys.
    """
    pred_dicts = [t.as_dict() for t in predicted]
    pred_clean = [_strip_prefix(t.syllable) for t in predicted]
    ref_clean = [_strip_prefix(r["syllable"]) for r in reference]

    matcher = difflib.SequenceMatcher(None, pred_clean, ref_clean, autojunk=False)

    deltas: list[float] = []
    comparison_rows: list[dict] = []

    for pred_start_idx, ref_start_idx, block_len in matcher.get_matching_blocks():
        for offset in range(block_len):
            pred_entry = pred_dicts[pred_start_idx + offset]
            ref_entry = reference[ref_start_idx + offset]

            delta = pred_entry["start"] - ref_entry["start"]
            deltas.append(abs(delta))

            comparison_rows.append({
                "syllable": _strip_prefix(ref_entry["syllable"]),
                "ref_start": ref_entry["start"],
                "pred_start": pred_entry["start"],
                "delta": delta,
            })

    if not deltas:
        return {
            "label": label,
            "mae": None,
            "hit_rate_01s": None,
            "matched_count": 0,
            "total_ref": len(reference),
            "first_20": [],
        }

    mae = sum(deltas) / len(deltas)
    hit_rate = sum(1 for d in deltas if d < 0.1) / len(deltas)

    print(f"\n    [{label}] Matched {len(deltas)}/{len(reference)} syllables.")
    print(f"    [{label}] MAE = {mae:.3f}s | Hit rate (<0.1s) = {hit_rate:.1%}")

    print(f"\n    {'Syllable':<12} | {'Ref start':>10} | "
          f"{'Pred start':>10} | {'Delta':>8}")
    print(f"    {'-'*12}-+-{'-'*10}-+-{'-'*10}-+-{'-'*8}")
    for row in comparison_rows[:20]:
        sign = "+" if row["delta"] >= 0 else ""
        print(
            f"    {row['syllable']:<12} | {row['ref_start']:>10.3f} | "
            f"{row['pred_start']:>10.3f} | {sign}{row['delta']:>7.3f}"
        )

    return {
        "label": label,
        "mae": mae,
        "hit_rate_01s": hit_rate,
        "matched_count": len(deltas),
        "total_ref": len(reference),
        "first_20": comparison_rows[:20],
    }


# ---------------------------------------------------------------------------
# Process one track
# ---------------------------------------------------------------------------


def process_track(
    meta: TrackMeta,
    alignment_model,
    alignment_tokenizer,
    syllabifier: Syllabifier,
) -> dict:
    """Run the hybrid CTC alignment pipeline on a single track.

    Steps:
      1. Load full audio.
      2. Compute emissions once (full track).
      3. Word-level alignment on full emissions.
      4. Per-word char-level alignment via emissions slicing (no re-inference).
      5. Return syllable timings + stats.

    Args:
        meta: Track metadata and file paths.
        alignment_model: Shared ONNX InferenceSession.
        alignment_tokenizer: Shared tokenizer.
        syllabifier: Shared Syllabifier instance.

    Returns:
        Dict with "timings", "syllable_count", "word_timestamp_count",
        "lyrics_word_count", and "hybrid_stats".
    """
    from ctc_forced_aligner import load_audio

    print(f"\n  --- Track {meta.track_num}: {meta.artist} - {meta.title} ---")

    lyrics_text = meta.lyrics_path.read_text(encoding="utf-8")
    lyrics_flat = lyrics_text.replace("\n", " ").strip()
    word_count = len(lyrics_flat.split())
    print(f"    Lyrics: {len(lyrics_flat)} chars, {word_count} words")

    print(f"    Loading audio from {meta.vocals_path} ...")
    audio_waveform = load_audio(str(meta.vocals_path), ret_type="np")
    print(f"    Audio waveform shape: {audio_waveform.shape}")

    # Step 1: compute emissions once for the entire track.
    emissions, stride_ms = compute_emissions(audio_waveform, alignment_model)

    # Step 2: word-level alignment on full emissions.
    word_timestamps = run_word_alignment(
        emissions,
        stride_ms,
        lyrics_flat,
        meta.language,
        alignment_tokenizer,
    )

    # Step 3: per-word char-level alignment via emissions slicing.
    print(f"\n    [Hybrid] Running char-level CTC per word "
          f"(slicing emissions, no re-inference) ...")
    syllable_timings, stats = build_hybrid_syllable_timings(
        word_timestamps=word_timestamps,
        lyrics_text=lyrics_text,
        emissions=emissions,
        stride_ms=stride_ms,
        language=meta.language,
        alignment_tokenizer=alignment_tokenizer,
        syllabifier=syllabifier,
    )

    total = stats["total_words"]
    char_used = stats["char_level_used"]
    fallback = stats["proportional_fallback"]
    print(f"    [Hybrid] Words: {total} "
          f"(char-level: {char_used}, proportional fallback: {fallback})")
    if stats["fallback_reasons"]:
        print(f"    [Hybrid] Sample fallback reasons (first 5):")
        for entry in stats["fallback_reasons"][:5]:
            print(f"      word='{entry['word']}': {entry['reason']}")

    print(f"    [Hybrid] Produced {len(syllable_timings)} syllable timings.")

    return {
        "timings": [t.as_dict() for t in syllable_timings],
        "syllable_count": len(syllable_timings),
        "word_timestamp_count": len(word_timestamps),
        "lyrics_word_count": word_count,
        "hybrid_stats": stats,
    }


# ---------------------------------------------------------------------------
# Load track metadata
# ---------------------------------------------------------------------------


def load_track_meta(track_num: int) -> TrackMeta:
    """Load metadata for a single test track.

    Args:
        track_num: Integer 1-5.

    Returns:
        Populated TrackMeta namedtuple.
    """
    track_dir = TEST_DATA_DIR / str(track_num)
    meta_raw = json.loads((track_dir / "meta.json").read_text())
    return TrackMeta(
        track_num=track_num,
        artist=meta_raw["artist"],
        title=meta_raw["title"],
        language=meta_raw["language"],
        track_id=meta_raw["track_id"],
        vocals_path=track_dir / "vocals.wav",
        lyrics_path=track_dir / "lyrics.txt",
        reference_path=track_dir / "reference_timings.json",
    )


# ---------------------------------------------------------------------------
# Summary writer
# ---------------------------------------------------------------------------


def _write_summary(all_results: dict) -> None:
    """Write a human-readable summary table to results/summary_hybrid.txt.

    Args:
        all_results: Dict keyed by track number string, values are per-track
            result dicts as stored in results_hybrid.json.
    """
    lines: list[str] = []
    lines.append("Variant CTC Experiment Summary (hybrid word+char level)")
    lines.append("=" * 75)
    lines.append("")
    lines.append(
        "Approach: word-level CTC (full emissions) -> per-word char-level CTC "
        "on emissions slice -> pyphen syllable boundaries"
    )
    lines.append("")
    lines.append(
        f"{'Track':<6} | {'Artist/Title':<38} | {'Lang':>4} | "
        f"{'MAE':>8} | {'Hit%':>7} | {'Matched':>9} | "
        f"{'CharLvl':>7} | {'Fallbk':>6}"
    )
    lines.append("-" * 90)

    all_maes: list[float] = []

    for track_num_str, result in all_results.items():
        if "error" in result and "meta" not in result:
            lines.append(f"{track_num_str:<6} | ERROR: {result['error']}")
            continue

        meta = result["meta"]
        short_title = f"{meta['artist']} - {meta['title']}"[:37]
        lang = meta["language"]

        metrics = result.get("hybrid", {})
        mae = metrics.get("mae")
        hit = metrics.get("hit_rate_01s")
        matched = metrics.get("matched_count", 0)
        total_ref = metrics.get("total_ref", 0)

        hstats = result.get("hybrid_stats", {})
        char_used = hstats.get("char_level_used", "-")
        fallback = hstats.get("proportional_fallback", "-")

        mae_str = f"{mae:.3f}s" if mae is not None else "N/A"
        hit_str = f"{hit:.1%}" if hit is not None else "N/A"
        matched_str = f"{matched}/{total_ref}"

        lines.append(
            f"{track_num_str:<6} | {short_title:<38} | {lang:>4} | "
            f"{mae_str:>8} | {hit_str:>7} | {matched_str:>9} | "
            f"{str(char_used):>7} | {str(fallback):>6}"
        )

        if mae is not None:
            all_maes.append(mae)

    lines.append("")
    if all_maes:
        avg_mae = sum(all_maes) / len(all_maes)
        lines.append(f"Average MAE: {avg_mae:.3f}s")
        good = sum(1 for m in all_maes if m < 0.15)
        lines.append(f"Tracks with MAE < 0.15s: {good}/{len(all_maes)}")

    summary_path = RESULTS_DIR / "summary_hybrid.txt"
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nSummary saved to {summary_path}")
    print("\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the hybrid CTC forced alignment experiment for all 5 test tracks."""
    from ctc_forced_aligner import AlignmentSingleton

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # Load model once — reused for all word-level and char-level calls.
    print("Loading CTC alignment model (ONNX) ...")
    aligner = AlignmentSingleton()
    alignment_model = aligner.alignment_model
    alignment_tokenizer = aligner.alignment_tokenizer
    print("CTC alignment model loaded.")

    syllabifier = Syllabifier()
    all_results: dict[str, dict] = {}

    for track_num in TRACK_IDS:
        meta = load_track_meta(track_num)
        print(f"\n{'='*60}")
        print(f"Processing track {track_num}: {meta.artist} - {meta.title}")
        print(f"{'='*60}")

        try:
            output = process_track(
                meta, alignment_model, alignment_tokenizer, syllabifier
            )
        except Exception as exc:
            import traceback
            print(f"  [ERROR] Track {track_num} failed: {exc}")
            traceback.print_exc()
            all_results[str(track_num)] = {
                "error": str(exc),
                "meta": {
                    "artist": meta.artist,
                    "title": meta.title,
                    "language": meta.language,
                },
            }
            continue

        reference = json.loads(meta.reference_path.read_text())
        predicted_timings = [
            SyllableTiming(
                syllable=t["syllable"],
                start=t["start"],
                end=t["end"],
            )
            for t in output["timings"]
        ]

        print(f"\n  === Evaluation: track {track_num} ===")
        metrics = evaluate_timings(predicted_timings, reference, "hybrid")

        all_results[str(track_num)] = {
            "meta": {
                "artist": meta.artist,
                "title": meta.title,
                "language": meta.language,
            },
            "syllable_count": output["syllable_count"],
            "word_timestamp_count": output["word_timestamp_count"],
            "lyrics_word_count": output["lyrics_word_count"],
            "hybrid": metrics,
            "hybrid_stats": output["hybrid_stats"],
            "hybrid_timings": output["timings"],
        }

    # Persist full results — never overwrite results.json or results_char.json.
    results_path = RESULTS_DIR / "results_hybrid.json"
    results_path.write_text(
        json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nResults saved to {results_path}")

    _write_summary(all_results)


if __name__ == "__main__":
    main()
