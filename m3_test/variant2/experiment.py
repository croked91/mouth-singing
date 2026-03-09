"""Variant 2 experiment: WhisperX ASR -> fuzzy align -> force_align syllables.

Pipeline per track:
  A. WhisperX ASR on vocals -> word-level timings (text ignored, timings kept)
  B. Fuzzy-align ASR words to known lyrics -> line-level timings
  C. Syllabify each line -> segments for force_align
  D. WhisperX force_align -> syllable-level timings
  E. Map results to SyllableTiming objects

Also runs the current fallback (path 2: ASR -> proportional split) as a baseline
so we can compare the two approaches on the same tracks.

Results saved to m3_test/variant2/results/.
"""

from __future__ import annotations

import difflib
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------
sys.path.insert(0, "/home/croked/karaoke/v2/bootstrap")
sys.path.insert(0, "/home/croked/karaoke/v2/shared")

from app.pipeline.whisperx_transcriber import WhisperXTranscriber  # noqa: E402
from karaoke_shared.models.track import SyllableTiming  # noqa: E402
from karaoke_shared.utils.syllabifier import Syllabifier  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TEST_DATA_DIR = Path("/home/croked/karaoke/m3_test/test_data")
RESULTS_DIR = Path("/home/croked/karaoke/m3_test/variant2/results")
TRACK_IDS = [1, 2, 3, 4, 5]
DEVICE = "cuda"


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


# ---------------------------------------------------------------------------
# Step A — WhisperX ASR
# ---------------------------------------------------------------------------


def run_asr(vocals_path: Path, language: str) -> list[dict]:
    """Run WhisperX ASR and return word-level timings.

    We only care about the timings, not the transcribed text.  The text
    from the ASR is discarded; we will use the known lyrics instead.

    Args:
        vocals_path: Path to the vocal WAV file.
        language: BCP-47 language code ("ru", "en").

    Returns:
        List of {"word": str, "start": float, "end": float} dicts.
    """
    print(f"    [ASR] Loading WhisperX transcriber (language={language}) ...")
    transcriber = WhisperXTranscriber(language=language, device=DEVICE)
    try:
        asr_words = transcriber.transcribe(vocals_path)
    finally:
        transcriber.cleanup()

    print(f"    [ASR] Got {len(asr_words)} words from ASR.")
    return asr_words


# ---------------------------------------------------------------------------
# Step B — Fuzzy alignment of ASR words to known lyrics
# ---------------------------------------------------------------------------


def _normalize_word(word: str) -> str:
    """Lowercase, strip punctuation, normalize unicode for comparison."""
    word = unicodedata.normalize("NFC", word).lower()
    word = re.sub(r"[^\w]", "", word, flags=re.UNICODE)
    return word


def _build_known_word_list(
    lines: list[str],
) -> tuple[list[str], list[int]]:
    """Flatten lyrics lines into a word list, tracking which line each word belongs to.

    Args:
        lines: Non-empty lines from lyrics.txt.

    Returns:
        A tuple of (flat_words, line_indices) where line_indices[i] gives the
        index into `lines` for word i.
    """
    flat_words: list[str] = []
    line_indices: list[int] = []

    for line_idx, line in enumerate(lines):
        for word in line.split():
            flat_words.append(word)
            line_indices.append(line_idx)

    return flat_words, line_indices


def _align_asr_to_known(
    asr_words: list[dict],
    known_words: list[str],
) -> list[dict | None]:
    """Use SequenceMatcher to align ASR words to known words by text similarity.

    For each position in `known_words`, returns the best-matched ASR word dict
    (with "start"/"end"), or None if no match was found in that region.

    The alignment works by comparing normalized word texts.  SequenceMatcher
    produces matching blocks; within each block we assign ASR words to known
    words positionally.

    Args:
        asr_words: Output of WhisperX ASR (each has "word", "start", "end").
        known_words: Flat list of words from the correct lyrics.

    Returns:
        List of same length as known_words.  Each element is either a matched
        ASR word dict or None.
    """
    asr_texts = [_normalize_word(w["word"]) for w in asr_words]
    known_texts = [_normalize_word(w) for w in known_words]

    matcher = difflib.SequenceMatcher(
        None, asr_texts, known_texts, autojunk=False
    )

    # matched_asr[i] = index into asr_words that aligns to known_words[i]
    matched_asr: list[int | None] = [None] * len(known_words)

    for asr_start, known_start, length in matcher.get_matching_blocks():
        for offset in range(length):
            matched_asr[known_start + offset] = asr_start + offset

    result: list[dict | None] = []
    for asr_idx in matched_asr:
        if asr_idx is not None:
            result.append(asr_words[asr_idx])
        else:
            result.append(None)

    return result


def _compute_line_timings(
    lines: list[str],
    matched_per_word: list[dict | None],
    line_indices: list[int],
    total_duration_hint: float,
) -> list[tuple[float, float]]:
    """Compute (start, end) timing for each lyrics line.

    Uses matched ASR words to anchor line boundaries.  Gaps caused by
    unmatched words are filled by linear interpolation between the nearest
    known anchors.

    Args:
        lines: Lyrics lines (same as passed to _build_known_word_list).
        matched_per_word: Per-word match result from _align_asr_to_known.
        line_indices: Which line each word belongs to (from _build_known_word_list).
        total_duration_hint: Duration of the audio in seconds (used as a
            fallback end time when the last lines have no matches).

    Returns:
        List of (start_sec, end_sec) tuples, one per line.
    """
    num_lines = len(lines)

    # Collect the first and last matched word timing per line.
    line_first: list[float | None] = [None] * num_lines
    line_last: list[float | None] = [None] * num_lines

    for word_idx, asr_word in enumerate(matched_per_word):
        if asr_word is None:
            continue
        line_idx = line_indices[word_idx]
        t_start = asr_word["start"]
        t_end = asr_word["end"]

        if line_first[line_idx] is None:
            line_first[line_idx] = t_start
        line_last[line_idx] = t_end

    # --- interpolate missing line_first values ---
    # Collect all known anchor points: (line_index, time)
    anchors: list[tuple[int, float]] = []
    for i in range(num_lines):
        if line_first[i] is not None:
            anchors.append((i, line_first[i]))  # type: ignore[arg-type]

    if not anchors:
        # No matches at all — divide audio duration equally.
        step = total_duration_hint / num_lines
        return [(i * step, (i + 1) * step) for i in range(num_lines)]

    # Fill in missing starts by linear interpolation between anchors.
    filled_first: list[float] = [0.0] * num_lines

    # Before first anchor: extrapolate backwards (clamp at 0).
    first_anchor_idx, first_anchor_time = anchors[0]
    for i in range(first_anchor_idx):
        filled_first[i] = max(0.0, first_anchor_time - (first_anchor_idx - i) * 2.0)

    # Between anchors: linear interpolation.
    for k in range(len(anchors) - 1):
        idx_a, time_a = anchors[k]
        idx_b, time_b = anchors[k + 1]
        filled_first[idx_a] = time_a
        gap_lines = idx_b - idx_a
        for j in range(1, gap_lines):
            frac = j / gap_lines
            filled_first[idx_a + j] = time_a + frac * (time_b - time_a)

    # After last anchor: extrapolate forwards (clamp at total_duration_hint).
    last_anchor_idx, last_anchor_time = anchors[-1]
    filled_first[last_anchor_idx] = last_anchor_time
    for i in range(last_anchor_idx + 1, num_lines):
        filled_first[i] = min(
            total_duration_hint,
            last_anchor_time + (i - last_anchor_idx) * 2.0,
        )

    # Build (start, end) pairs.  The end of line i is the start of line i+1.
    line_timings: list[tuple[float, float]] = []
    for i in range(num_lines):
        start = filled_first[i]
        if i + 1 < num_lines:
            end = filled_first[i + 1]
        else:
            # Last line: use the last matched word end or total duration.
            end = line_last[i] if line_last[i] is not None else total_duration_hint

        # Ensure minimum line width of 0.5s and end > start.
        end = max(end, start + 0.5)
        line_timings.append((start, end))

    return line_timings


def compute_line_timings_from_asr(
    asr_words: list[dict],
    lyrics_text: str,
    audio_duration: float,
) -> tuple[list[str], list[tuple[float, float]]]:
    """Align ASR timings to known lyrics and return per-line timings.

    Args:
        asr_words: Word-level ASR output (text + start/end times).
        lyrics_text: Full lyrics text (newline-separated lines).
        audio_duration: Total audio duration in seconds (used as fallback).

    Returns:
        Tuple of (lines, line_timings) where:
        - lines: Non-empty lyrics lines.
        - line_timings: (start_sec, end_sec) for each line.
    """
    lines = [ln for ln in lyrics_text.splitlines() if ln.strip()]
    flat_words, line_indices = _build_known_word_list(lines)

    matched_per_word = _align_asr_to_known(asr_words, flat_words)

    matched_count = sum(1 for m in matched_per_word if m is not None)
    print(
        f"    [Align] Matched {matched_count}/{len(flat_words)} known words "
        f"to ASR words."
    )

    line_timings = _compute_line_timings(
        lines, matched_per_word, line_indices, audio_duration
    )
    return lines, line_timings


# ---------------------------------------------------------------------------
# Step C — Build segments for force_align
# ---------------------------------------------------------------------------


def build_force_align_segments(
    lines: list[str],
    line_timings: list[tuple[float, float]],
    language: str,
    syllabifier: Syllabifier,
) -> tuple[list[dict], list[str], list[bool], list[bool]]:
    """Convert line timings into WhisperX force_align segment dicts.

    Each line is syllabified and its syllables are joined with spaces to form
    a "segment text" that WhisperX will align word-by-word — one word per
    syllable.

    Args:
        lines: Lyrics lines.
        line_timings: (start_sec, end_sec) per line from Step B.
        language: Language code for syllabification.
        syllabifier: Shared Syllabifier instance.

    Returns:
        A 4-tuple:
        - segments: List of {"text": str, "start": float, "end": float} for
          WhisperX force_align.
        - all_syl_strings: Flat list of syllable strings in order.
        - all_is_word_start: True where a syllable is the first of a word.
        - all_is_line_start: True where a syllable is the first of a line.
    """
    segments: list[dict] = []
    all_syl_strings: list[str] = []
    all_is_word_start: list[bool] = []
    all_is_line_start: list[bool] = []

    for line, (start, end) in zip(lines, line_timings):
        syl_strings, is_word_start = syllabifier.split_text_to_syllables(
            line, language
        )
        if not syl_strings:
            continue

        syl_text = " ".join(syl_strings)
        segments.append({"text": syl_text, "start": start, "end": end})

        line_flags = [False] * len(syl_strings)
        line_flags[0] = True

        all_syl_strings.extend(syl_strings)
        all_is_word_start.extend(is_word_start)
        all_is_line_start.extend(line_flags)

    return segments, all_syl_strings, all_is_word_start, all_is_line_start


# ---------------------------------------------------------------------------
# Step D — WhisperX force_align
# ---------------------------------------------------------------------------


def run_force_align(
    vocals_path: Path,
    segments: list[dict],
    language: str,
) -> list[dict]:
    """Run WhisperX force_align on syllable segments.

    Uses a fresh WhisperXTranscriber (align model only — no ASR model loaded).

    Args:
        vocals_path: Path to the vocal WAV file.
        segments: Segment dicts from build_force_align_segments.
        language: Language code for the alignment model.

    Returns:
        Word-level timestamps from force_align (one entry per aligned syllable).
    """
    print(f"    [ForceAlign] Aligning {len(segments)} segments ...")
    transcriber = WhisperXTranscriber(language=language, device=DEVICE)
    try:
        syl_timestamps = transcriber.force_align(vocals_path, segments)
    finally:
        transcriber.cleanup()

    print(f"    [ForceAlign] Got {len(syl_timestamps)} syllable timestamps.")
    return syl_timestamps


# ---------------------------------------------------------------------------
# Step E — Map force_align output to SyllableTiming (copied from bootstrap_runner.py)
# ---------------------------------------------------------------------------


def _map_syllable_timestamps(
    whisperx_words: list[dict],
    expected_syllables: list[str],
    is_word_start: list[bool],
    is_line_start: list[bool] | None = None,
) -> list[SyllableTiming]:
    """Map WhisperX force-align output to SyllableTiming objects.

    WhisperX may drop some "words" (syllables) that it cannot align.
    We match by position and add prefixes for word/line boundaries so
    that rendered karaoke text has proper spacing and line breaks.

    Prefix conventions:
    - " " (space) marks the first syllable of a new word.
    - "\\n" marks the first syllable of a new LRC line (implies word boundary).

    Copied from bootstrap_runner.py lines 272-327.

    Args:
        whisperx_words: Output of WhisperXTranscriber.force_align().
        expected_syllables: The syllable strings sent to WhisperX.
        is_word_start: Boolean flags — True marks the first syllable of a word.
        is_line_start: Optional boolean flags — True marks the first syllable
            of a new line.

    Returns:
        List of SyllableTiming instances.
    """
    timings: list[SyllableTiming] = []

    for i, word_info in enumerate(whisperx_words):
        if i >= len(expected_syllables):
            break

        syllable_text = word_info["word"]

        if i > 0:
            if is_line_start and i < len(is_line_start) and is_line_start[i]:
                syllable_text = "\n" + syllable_text
            elif i < len(is_word_start) and is_word_start[i]:
                syllable_text = " " + syllable_text

        timings.append(
            SyllableTiming(
                syllable=syllable_text,
                start=float(word_info["start"]),
                end=float(word_info["end"]),
            )
        )

    return timings


# ---------------------------------------------------------------------------
# Baseline: path 2 (ASR -> proportional syllable split)
# ---------------------------------------------------------------------------


class _FakeToken:
    """Adapter to make WhisperX word dicts work with Syllabifier._from_word_tokens."""

    def __init__(self, word: dict, language: str) -> None:
        self.text = word["word"]
        self.start_ms = word["start"] * 1000
        self.end_ms = word["end"] * 1000
        self.language = language


def compute_baseline_timings(
    asr_words: list[dict],
    language: str,
    syllabifier: Syllabifier,
) -> list[SyllableTiming]:
    """Replicate the current fallback (path 2): proportional syllable split.

    Each ASR word is split into syllables by pyphen, and the word's time span
    is divided proportionally among its syllables by character count.

    Args:
        asr_words: Word-level ASR output.
        language: Language code.
        syllabifier: Shared Syllabifier instance.

    Returns:
        List of SyllableTiming with proportionally-distributed timings.
    """
    tokens = [_FakeToken(w, language) for w in asr_words]
    # _from_word_tokens is the proportional split used in the fallback path.
    return syllabifier._from_word_tokens(tokens)  # noqa: SLF001


# ---------------------------------------------------------------------------
# Audio duration helper
# ---------------------------------------------------------------------------


def get_audio_duration(audio_path: Path) -> float:
    """Return the duration of an audio file in seconds using soundfile.

    Falls back to a generous estimate (600s) if soundfile is unavailable.

    Args:
        audio_path: Path to a WAV or MP3 file.

    Returns:
        Duration in seconds.
    """
    try:
        import soundfile as sf

        info = sf.info(str(audio_path))
        return info.duration
    except Exception:
        pass

    try:
        import librosa

        duration = librosa.get_duration(path=str(audio_path))
        return float(duration)
    except Exception:
        pass

    print("    [Warning] Could not determine audio duration — using 600s fallback.")
    return 600.0


# ---------------------------------------------------------------------------
# Process one track
# ---------------------------------------------------------------------------


def process_track(meta: TrackMeta) -> dict:
    """Run the full Variant 2 pipeline on a single track.

    Returns a dict with keys:
    - "variant2": list of SyllableTiming dicts (serialisable)
    - "baseline": list of SyllableTiming dicts
    - "asr_word_count": int
    - "matched_word_count": int  (filled in by compute_line_timings_from_asr)
    - "line_count": int
    """
    print(f"\n  --- Track {meta.track_num}: {meta.artist} - {meta.title} ---")

    lyrics_text = meta.lyrics_path.read_text(encoding="utf-8")
    audio_duration = get_audio_duration(meta.vocals_path)
    print(f"    Audio duration: {audio_duration:.1f}s")

    syllabifier = Syllabifier()

    # ------------------------------------------------------------------
    # Step A: WhisperX ASR
    # ------------------------------------------------------------------
    asr_words = run_asr(meta.vocals_path, meta.language)

    if not asr_words:
        print("    [Warning] ASR returned no words — skipping this track.")
        return {
            "error": "ASR returned no words",
            "variant2": [],
            "baseline": [],
        }

    # ------------------------------------------------------------------
    # Baseline (path 2) — computed from ASR words before anything else
    # so we don't need a second ASR pass.
    # ------------------------------------------------------------------
    print("    [Baseline] Computing proportional syllable split ...")
    baseline_timings = compute_baseline_timings(asr_words, meta.language, syllabifier)
    print(f"    [Baseline] {len(baseline_timings)} syllable timings produced.")

    # ------------------------------------------------------------------
    # Step B: Fuzzy alignment
    # ------------------------------------------------------------------
    lines, line_timings = compute_line_timings_from_asr(
        asr_words, lyrics_text, audio_duration
    )
    print(f"    [Align] {len(lines)} lines, timings: "
          f"{line_timings[0]} ... {line_timings[-1]}")

    # ------------------------------------------------------------------
    # Step C: Build force_align segments
    # ------------------------------------------------------------------
    segments, all_syl_strings, all_is_word_start, all_is_line_start = (
        build_force_align_segments(lines, line_timings, meta.language, syllabifier)
    )
    print(f"    [Segments] {len(segments)} segments, "
          f"{len(all_syl_strings)} total syllables expected.")

    # ------------------------------------------------------------------
    # Step D: WhisperX force_align (fresh instance, alignment model only)
    # ------------------------------------------------------------------
    syl_timestamps = run_force_align(meta.vocals_path, segments, meta.language)

    # ------------------------------------------------------------------
    # Step E: Map to SyllableTiming
    # ------------------------------------------------------------------
    syllable_timings = _map_syllable_timestamps(
        syl_timestamps, all_syl_strings, all_is_word_start, all_is_line_start
    )
    print(f"    [Map] {len(syllable_timings)} SyllableTiming objects produced.")

    return {
        "variant2": [st.model_dump() for st in syllable_timings],
        "baseline": [st.model_dump() for st in baseline_timings],
        "asr_word_count": len(asr_words),
        "line_count": len(lines),
    }


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _strip_prefix(syllable: str) -> str:
    """Remove space or newline prefix added as a word/line boundary marker."""
    return syllable.lstrip(" \n")


def evaluate_timings(
    predicted: list[dict],
    reference: list[dict],
    label: str,
) -> dict:
    """Compare predicted syllable timings to reference timings.

    Alignment is done by matching cleaned syllable text.  We walk both lists
    in order and try to find the same syllable in the other list (within a
    small lookahead window) to handle dropped or extra syllables.

    Args:
        predicted: List of {"syllable": str, "start": float, ...} dicts.
        reference: Same format — the ground-truth timings.
        label: Short name printed in the comparison table (e.g. "variant2").

    Returns:
        Dict with "mae", "hit_rate_01s", "matched_count", "total_ref" keys
        and a "first_20" list of comparison rows for display.
    """
    # Build cleaned text lists for matching.
    pred_clean = [_strip_prefix(s["syllable"]) for s in predicted]
    ref_clean = [_strip_prefix(s["syllable"]) for s in reference]

    # Use SequenceMatcher to align predicted to reference by syllable text.
    matcher = difflib.SequenceMatcher(None, pred_clean, ref_clean, autojunk=False)

    deltas: list[float] = []
    comparison_rows: list[dict] = []

    for pred_start_idx, ref_start_idx, block_len in matcher.get_matching_blocks():
        for offset in range(block_len):
            pred_entry = predicted[pred_start_idx + offset]
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

    # Print comparison table for first 20 matched syllables.
    print(f"\n    {'Syllable':<12} | {'Ref start':>10} | {'Pred start':>10} | {'Delta':>8}")
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
# Main
# ---------------------------------------------------------------------------


def load_track_meta(track_num: int) -> TrackMeta:
    """Load metadata for a single test track."""
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


def main() -> None:
    """Run the experiment for all 5 test tracks and save results."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    all_results: dict[str, dict] = {}

    for track_num in TRACK_IDS:
        meta = load_track_meta(track_num)
        print(f"\n{'='*60}")
        print(f"Processing track {track_num}: {meta.artist} - {meta.title}")
        print(f"{'='*60}")

        try:
            output = process_track(meta)
        except Exception as exc:
            print(f"  [ERROR] Track {track_num} failed: {exc}")
            all_results[str(track_num)] = {"error": str(exc)}
            continue

        reference = json.loads(meta.reference_path.read_text())

        print(f"\n  === Evaluation: track {track_num} ===")

        v2_metrics = evaluate_timings(output["variant2"], reference, "variant2")
        baseline_metrics = evaluate_timings(output["baseline"], reference, "baseline")

        all_results[str(track_num)] = {
            "meta": {
                "artist": meta.artist,
                "title": meta.title,
                "language": meta.language,
            },
            "asr_word_count": output.get("asr_word_count"),
            "line_count": output.get("line_count"),
            "variant2": v2_metrics,
            "baseline": baseline_metrics,
            # Save produced timings for inspection.
            "variant2_timings": output["variant2"],
            "baseline_timings": output["baseline"],
        }

    # Save full results JSON.
    results_path = RESULTS_DIR / "results.json"
    results_path.write_text(
        json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n\nResults saved to {results_path}")

    # Write summary text.
    _write_summary(all_results)


def _write_summary(all_results: dict) -> None:
    """Write a human-readable summary of the experiment results."""
    lines: list[str] = []
    lines.append("Variant 2 Experiment Summary")
    lines.append("=" * 60)
    lines.append("")
    lines.append(
        "Approach: WhisperX ASR -> fuzzy align to known lyrics -> "
        "force_align syllables"
    )
    lines.append(
        "Baseline: WhisperX ASR -> pyphen proportional syllable split"
    )
    lines.append("")
    lines.append(
        f"{'Track':<6} | {'Artist/Title':<35} | {'Lang':>4} | "
        f"{'V2 MAE':>8} | {'V2 Hit%':>7} | "
        f"{'Base MAE':>8} | {'Base Hit%':>9}"
    )
    lines.append("-" * 90)

    v2_maes: list[float] = []
    base_maes: list[float] = []

    for track_num_str, result in all_results.items():
        if "error" in result and "meta" not in result:
            lines.append(f"{track_num_str:<6} | ERROR: {result['error']}")
            continue

        meta = result["meta"]
        short_title = f"{meta['artist']} - {meta['title']}"[:34]
        lang = meta["language"]

        v2 = result.get("variant2", {})
        base = result.get("baseline", {})

        v2_mae = v2.get("mae")
        base_mae = base.get("mae")
        v2_hit = v2.get("hit_rate_01s")
        base_hit = base.get("hit_rate_01s")

        v2_mae_str = f"{v2_mae:.3f}s" if v2_mae is not None else "N/A"
        base_mae_str = f"{base_mae:.3f}s" if base_mae is not None else "N/A"
        v2_hit_str = f"{v2_hit:.1%}" if v2_hit is not None else "N/A"
        base_hit_str = f"{base_hit:.1%}" if base_hit is not None else "N/A"

        lines.append(
            f"{track_num_str:<6} | {short_title:<35} | {lang:>4} | "
            f"{v2_mae_str:>8} | {v2_hit_str:>7} | "
            f"{base_mae_str:>8} | {base_hit_str:>9}"
        )

        if v2_mae is not None:
            v2_maes.append(v2_mae)
        if base_mae is not None:
            base_maes.append(base_mae)

    lines.append("")
    if v2_maes:
        avg_v2 = sum(v2_maes) / len(v2_maes)
        lines.append(f"Average Variant 2 MAE: {avg_v2:.3f}s")
    if base_maes:
        avg_base = sum(base_maes) / len(base_maes)
        lines.append(f"Average Baseline MAE:  {avg_base:.3f}s")

    lines.append("")
    lines.append("Verdict:")
    if v2_maes and base_maes:
        avg_v2 = sum(v2_maes) / len(v2_maes)
        avg_base = sum(base_maes) / len(base_maes)
        if avg_v2 < avg_base:
            improvement_pct = (avg_base - avg_v2) / avg_base * 100
            lines.append(
                f"  Variant 2 is BETTER than baseline by "
                f"{improvement_pct:.0f}% on average MAE."
            )
        else:
            lines.append(
                "  Variant 2 did NOT improve over baseline on average MAE."
            )

        good_tracks = sum(1 for mae in v2_maes if mae < 0.15)
        lines.append(
            f"  Variant 2 meets MAE < 0.15s target on "
            f"{good_tracks}/{len(v2_maes)} tracks."
        )

    summary_path = RESULTS_DIR / "summary.txt"
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Summary saved to {summary_path}")
    print("\n" + "\n".join(lines))


if __name__ == "__main__":
    main()
