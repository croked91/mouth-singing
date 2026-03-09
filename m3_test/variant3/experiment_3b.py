"""Variant 3b: Deterministic syllable timing correction via difflib.

Approach:
  1. Run WhisperX ASR on the vocal track (emulating Sonoix word-level output).
  2. Align ASR words to the known correct lyrics via difflib.SequenceMatcher.
  3. Split matched words into syllables (pyphen) with proportional time sharing.
  4. Evaluate against reference timings (MAE, hit rate, WER).

Usage:
    source /home/croked/miniforge3/etc/profile.d/conda.sh && conda activate bootstrap
    python /home/croked/karaoke/m3_test/variant3/experiment_3b.py
"""

from __future__ import annotations

import difflib
import json
import re
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------
sys.path.insert(0, "/home/croked/karaoke/v2/bootstrap")
sys.path.insert(0, "/home/croked/karaoke/v2/shared")

from app.pipeline.whisperx_transcriber import WhisperXTranscriber
from karaoke_shared.models.track import SyllableTiming
from karaoke_shared.utils.syllabifier import Syllabifier

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TEST_DATA_ROOT = Path("/home/croked/karaoke/m3_test/test_data")
RESULTS_DIR = Path("/home/croked/karaoke/m3_test/variant3/results")
TRACK_IDS = [1, 2, 3, 4, 5]

# WhisperX model to use for ASR (medium is a good tradeoff for speed/quality)
WHISPERX_MODEL = "medium"


# ---------------------------------------------------------------------------
# Step B: Align ASR words to known lyrics with difflib
# ---------------------------------------------------------------------------

def _normalize_word(word: str) -> str:
    """Strip punctuation and lowercase for comparison."""
    return re.sub(r"[^\w]", "", word, flags=re.UNICODE).lower()


def align_asr_to_known(
    asr_words: list[dict],
    known_words: list[str],
) -> list[tuple[str, float, float] | None]:
    """Align ASR word list to the known correct word list.

    Uses difflib.SequenceMatcher on normalized word texts to compute opcodes,
    then assigns start/end timestamps to each known word.

    Args:
        asr_words: List of {"word": str, "start": float, "end": float}.
        known_words: Correct word list in order (from lyrics.txt).

    Returns:
        List of (word, start_sec, end_sec) or None for each known word.
        None means the word had no corresponding ASR output and timing
        must be interpolated from neighbours.
    """
    asr_texts = [_normalize_word(w["word"]) for w in asr_words]
    known_texts = [_normalize_word(w) for w in known_words]

    matcher = difflib.SequenceMatcher(None, asr_texts, known_texts, autojunk=False)
    opcodes = matcher.get_opcodes()

    # Allocate result slots — one per known word, initially None.
    result: list[tuple[str, float, float] | None] = [None] * len(known_words)

    for tag, asr_i1, asr_i2, kn_i1, kn_i2 in opcodes:
        if tag == "equal":
            # Perfect match: each known word gets its ASR word's timing.
            for offset in range(kn_i2 - kn_i1):
                kn_idx = kn_i1 + offset
                asr_idx = asr_i1 + offset
                asr_w = asr_words[asr_idx]
                result[kn_idx] = (known_words[kn_idx], asr_w["start"], asr_w["end"])

        elif tag == "replace":
            # Different words in ASR vs known. Take the time span of the
            # replaced ASR words and divide it proportionally among the
            # known words (by character length).
            asr_span_start = asr_words[asr_i1]["start"]
            asr_span_end = asr_words[asr_i2 - 1]["end"]
            _distribute_time_to_known(
                known_words, kn_i1, kn_i2, asr_span_start, asr_span_end, result
            )

        elif tag == "insert":
            # Words in known but absent from ASR. We leave them as None here;
            # they will be handled by interpolation after the main loop.
            pass

        elif tag == "delete":
            # Extra words in ASR that have no match in known — skip them.
            pass

    # Interpolate timings for known words that got no assignment (inserts).
    _interpolate_missing(result, known_words)

    return result


def _distribute_time_to_known(
    known_words: list[str],
    kn_i1: int,
    kn_i2: int,
    span_start: float,
    span_end: float,
    result: list,
) -> None:
    """Distribute a time span across a slice of known words proportionally."""
    span_words = known_words[kn_i1:kn_i2]
    if not span_words:
        return

    char_lengths = [max(len(_normalize_word(w)), 1) for w in span_words]
    total_chars = sum(char_lengths)
    span_duration = span_end - span_start
    cursor = span_start

    for offset, word in enumerate(span_words):
        fraction = char_lengths[offset] / total_chars
        word_end = cursor + span_duration * fraction
        result[kn_i1 + offset] = (word, cursor, word_end)
        cursor = word_end


def _interpolate_missing(
    result: list[tuple[str, float, float] | None],
    known_words: list[str],
) -> None:
    """Fill None slots by linear interpolation between neighbouring timings.

    Words at the very start or end with no neighbours get a zero-duration
    placeholder at the nearest known boundary.
    """
    n = len(result)
    if n == 0:
        return

    # Find the first and last non-None entries for boundary clamping.
    first_valid = next((i for i, r in enumerate(result) if r is not None), None)
    last_valid = next((i for i, r in enumerate(reversed(result)) if r is not None), None)

    if first_valid is None:
        # No timing info at all — assign zero-duration timings at t=0.
        for i, word in enumerate(known_words):
            result[i] = (word, 0.0, 0.0)
        return

    last_valid_idx = n - 1 - last_valid  # type: ignore[operator]

    i = 0
    while i < n:
        if result[i] is not None:
            i += 1
            continue

        # Find the gap: all consecutive None entries.
        gap_start = i
        while i < n and result[i] is None:
            i += 1
        gap_end = i  # exclusive

        # Determine boundary times.
        if gap_start == 0:
            left_time = result[last_valid_idx][1]  # type: ignore[index]
        else:
            left_time = result[gap_start - 1][2]  # type: ignore[index]

        if gap_end >= n:
            right_time = left_time
        else:
            right_time = result[gap_end][1]  # type: ignore[index]

        gap_size = gap_end - gap_start
        step = (right_time - left_time) / (gap_size + 1)

        for offset in range(gap_size):
            idx = gap_start + offset
            word_start = left_time + step * (offset + 1)
            word_end = left_time + step * (offset + 2)
            result[idx] = (known_words[idx], word_start, word_end)


# ---------------------------------------------------------------------------
# Step C: Split words into syllables with markers
# ---------------------------------------------------------------------------

def words_to_syllable_timings(
    aligned_words: list[tuple[str, float, float] | None],
    lyrics_lines: list[str],
    language: str,
    syllabifier: Syllabifier,
) -> list[SyllableTiming]:
    """Convert aligned word timings into syllable-level SyllableTiming list.

    Adds a space prefix to the first syllable of each word (except the very
    first syllable of the track), and a newline prefix at the start of each
    new lyrics line.

    Args:
        aligned_words: List of (word, start_sec, end_sec) from align_asr_to_known().
        lyrics_lines: Original lyrics split by line (to determine line breaks).
        language: Language code ("ru" or "en").
        syllabifier: Syllabifier instance.

    Returns:
        Flat list of SyllableTiming objects.
    """
    # Build a flat list of known words with their line index.
    words_with_line: list[tuple[str, int]] = []
    for line_idx, line in enumerate(lyrics_lines):
        for word in line.split():
            if word:
                words_with_line.append((word, line_idx))

    timings: list[SyllableTiming] = []
    is_first_syllable_overall = True

    for word_idx, aligned in enumerate(aligned_words):
        if aligned is None:
            continue

        word_text, word_start, word_end = aligned
        _, line_idx = words_with_line[word_idx]

        # Determine whether this word starts a new line relative to previous word.
        is_line_start = False
        if word_idx > 0:
            _, prev_line_idx = words_with_line[word_idx - 1]
            is_line_start = (line_idx != prev_line_idx)

        syllable_parts = syllabifier._split_word(word_text, language)
        if not syllable_parts:
            continue

        duration = word_end - word_start
        char_lengths = [max(len(s.strip()), 1) for s in syllable_parts]
        total_chars = sum(char_lengths)
        cursor = word_start

        for syl_idx, syl_text in enumerate(syllable_parts):
            fraction = char_lengths[syl_idx] / total_chars
            syl_end = cursor + duration * fraction

            # Add prefix marker on the first syllable of the word.
            if syl_idx == 0 and not is_first_syllable_overall:
                if is_line_start:
                    syl_text = "\n" + syl_text
                else:
                    syl_text = " " + syl_text

            timings.append(SyllableTiming(syllable=syl_text, start=cursor, end=syl_end))
            cursor = syl_end
            is_first_syllable_overall = False

    return timings


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def compute_wer(hypothesis_words: list[str], reference_words: list[str]) -> float:
    """Compute Word Error Rate via dynamic programming."""
    n = len(reference_words)
    m = len(hypothesis_words)

    # dp[i][j] = edit distance between ref[:i] and hyp[:j]
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if reference_words[i - 1] == hypothesis_words[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                dp[i][j] = 1 + min(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1])

    return dp[n][m] / max(n, 1)


def extract_syllable_text_words(timings: list[SyllableTiming]) -> list[str]:
    """Reconstruct word list from syllable timings by concatenating and splitting."""
    full_text = "".join(t.syllable for t in timings)
    # Replace newlines with spaces, then split on spaces.
    full_text = full_text.replace("\n", " ")
    words = full_text.split()
    return [_normalize_word(w) for w in words if _normalize_word(w)]


def evaluate_timings(
    predicted: list[SyllableTiming],
    reference: list[SyllableTiming],
    hit_threshold: float = 0.1,
) -> dict:
    """Compare predicted syllable timings to reference.

    Aligns predicted and reference by syllable position (index), then computes:
    - mae: mean absolute error on start times
    - hit_rate: fraction of syllables with |delta_start| < hit_threshold
    - alignment_count: number of syllables compared
    - table: first 20 rows of comparison
    """
    compare_count = min(len(predicted), len(reference))
    if compare_count == 0:
        return {"mae": None, "hit_rate": None, "alignment_count": 0, "table": []}

    deltas = []
    table_rows = []

    for i in range(compare_count):
        ref_syl = reference[i]
        pred_syl = predicted[i]
        delta = abs(pred_syl.start - ref_syl.start)
        deltas.append(delta)

        if i < 20:
            table_rows.append({
                "syllable_ref": ref_syl.syllable,
                "syllable_pred": pred_syl.syllable,
                "ref_start": round(ref_syl.start, 3),
                "pred_start": round(pred_syl.start, 3),
                "delta": round(delta, 3),
            })

    mae = sum(deltas) / len(deltas)
    hit_rate = sum(1 for d in deltas if d < hit_threshold) / len(deltas)

    return {
        "mae": round(mae, 4),
        "hit_rate": round(hit_rate, 4),
        "alignment_count": compare_count,
        "table": table_rows,
    }


# ---------------------------------------------------------------------------
# Main per-track pipeline
# ---------------------------------------------------------------------------

def process_track(track_id: int, track_dir: Path, syllabifier: Syllabifier) -> dict:
    """Run the full 3b pipeline for one track.

    Returns a dict with metrics and diagnostic info.
    """
    print(f"\n{'='*60}")
    print(f"  Processing track {track_id}: {track_dir}")
    print(f"{'='*60}")

    # Load metadata and inputs.
    meta = json.loads((track_dir / "meta.json").read_text())
    language = meta["language"]
    lyrics_text = (track_dir / "lyrics.txt").read_text(encoding="utf-8")
    lyrics_lines = [line for line in lyrics_text.splitlines() if line.strip()]
    vocals_path = track_dir / "vocals.wav"

    reference_data = json.loads((track_dir / "reference_timings.json").read_text())
    reference_timings = [SyllableTiming(**item) for item in reference_data]

    known_words: list[str] = []
    for line in lyrics_lines:
        known_words.extend(line.split())

    print(f"  Artist: {meta['artist']} — {meta['title']}")
    print(f"  Language: {language}")
    print(f"  Known words: {len(known_words)}, reference syllables: {len(reference_timings)}")

    # Step A: WhisperX ASR.
    print(f"\n  [A] Running WhisperX ASR ({WHISPERX_MODEL}) ...")
    asr_start = time.time()

    transcriber = WhisperXTranscriber(
        model_name=WHISPERX_MODEL,
        language=language,
        device="cuda",
    )
    asr_words = transcriber.transcribe(vocals_path)
    transcriber.cleanup()

    asr_elapsed = time.time() - asr_start
    print(f"  [A] Done: {len(asr_words)} ASR words in {asr_elapsed:.1f}s")

    if not asr_words:
        print("  [A] ERROR: no ASR output — skipping track")
        return {"track_id": track_id, "error": "no ASR output"}

    # Step B: Align ASR words to known lyrics.
    print(f"\n  [B] Aligning {len(asr_words)} ASR words to {len(known_words)} known words ...")
    aligned = align_asr_to_known(asr_words, known_words)

    assigned_count = sum(1 for r in aligned if r is not None)
    print(f"  [B] Assigned: {assigned_count}/{len(known_words)} words")

    # Step C: Split into syllables.
    print(f"\n  [C] Splitting to syllables ...")
    predicted_timings = words_to_syllable_timings(
        aligned, lyrics_lines, language, syllabifier
    )
    print(f"  [C] Predicted syllables: {len(predicted_timings)}")

    # Evaluation: timing quality.
    metrics = evaluate_timings(predicted_timings, reference_timings)
    print(f"\n  [EVAL] MAE: {metrics['mae']}s, Hit rate: {metrics['hit_rate']:.1%}, Compared: {metrics['alignment_count']} syllables")

    # Evaluation: WER.
    pred_words = extract_syllable_text_words(predicted_timings)
    ref_words = extract_syllable_text_words(reference_timings)
    wer = compute_wer(pred_words, ref_words)
    print(f"  [EVAL] WER: {wer:.1%} (pred {len(pred_words)} words vs ref {len(ref_words)} words)")

    # ASR WER (before correction — just the raw ASR text vs known).
    asr_texts_normalized = [_normalize_word(w["word"]) for w in asr_words if _normalize_word(w["word"])]
    known_normalized = [_normalize_word(w) for w in known_words if _normalize_word(w)]
    asr_wer = compute_wer(asr_texts_normalized, known_normalized)
    print(f"  [EVAL] ASR WER (before alignment): {asr_wer:.1%}")

    # Print comparison table.
    print(f"\n  First 20 syllables comparison:")
    print(f"  {'Ref syllable':<15} {'Pred syllable':<15} {'Ref start':>10} {'Pred start':>10} {'Delta':>8}")
    print(f"  {'-'*15} {'-'*15} {'-'*10} {'-'*10} {'-'*8}")
    for row in metrics["table"]:
        ref_syl = repr(row["syllable_ref"])[:14]
        pred_syl = repr(row["syllable_pred"])[:14]
        print(f"  {ref_syl:<15} {pred_syl:<15} {row['ref_start']:>10.3f} {row['pred_start']:>10.3f} {row['delta']:>8.3f}")

    return {
        "track_id": track_id,
        "artist": meta["artist"],
        "title": meta["title"],
        "language": language,
        "asr_word_count": len(asr_words),
        "known_word_count": len(known_words),
        "predicted_syllable_count": len(predicted_timings),
        "reference_syllable_count": len(reference_timings),
        "asr_elapsed_sec": round(asr_elapsed, 1),
        "mae": metrics["mae"],
        "hit_rate_01s": metrics["hit_rate"],
        "alignment_count": metrics["alignment_count"],
        "wer_after_alignment": round(wer, 4),
        "wer_before_alignment": round(asr_wer, 4),
        "comparison_table": metrics["table"],
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run experiment 3b on all 5 test tracks."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    syllabifier = Syllabifier()
    all_results = []

    for track_id in TRACK_IDS:
        track_dir = TEST_DATA_ROOT / str(track_id)
        if not track_dir.exists():
            print(f"Track {track_id} directory not found, skipping.")
            continue

        try:
            result = process_track(track_id, track_dir, syllabifier)
            all_results.append(result)
        except Exception as exc:
            print(f"\nERROR processing track {track_id}: {exc}")
            import traceback
            traceback.print_exc()
            all_results.append({"track_id": track_id, "error": str(exc)})

    # Save results JSON.
    results_path = RESULTS_DIR / "results.json"
    results_path.write_text(
        json.dumps(all_results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nResults saved to {results_path}")

    # Write summary.
    _write_summary(all_results)


def _write_summary(results: list[dict]) -> None:
    """Write a human-readable summary to results/summary.txt."""
    successful = [r for r in results if "error" not in r]
    failed = [r for r in results if "error" in r]

    lines = [
        "Experiment 3b: WhisperX + difflib alignment",
        "=" * 50,
        "",
        f"Tracks processed: {len(successful)}/{len(results)} successful",
        "",
        "Per-track results:",
        "",
    ]

    for r in successful:
        lines.append(f"  Track {r['track_id']}: {r['artist']} — {r['title']}")
        lines.append(f"    Language:      {r['language']}")
        lines.append(f"    MAE:           {r['mae']}s")
        lines.append(f"    Hit rate <0.1s: {r['hit_rate_01s']:.1%}")
        lines.append(f"    WER (ASR raw): {r['wer_before_alignment']:.1%}")
        lines.append(f"    WER (aligned): {r['wer_after_alignment']:.1%}")
        lines.append(f"    ASR time:      {r['asr_elapsed_sec']}s")
        lines.append(f"    Syllables:     pred={r['predicted_syllable_count']} ref={r['reference_syllable_count']}")
        lines.append("")

    if failed:
        lines.append("Failed tracks:")
        for r in failed:
            lines.append(f"  Track {r['track_id']}: {r.get('error', 'unknown error')}")
        lines.append("")

    if successful:
        avg_mae = sum(r["mae"] for r in successful) / len(successful)
        avg_hit = sum(r["hit_rate_01s"] for r in successful) / len(successful)
        avg_wer_after = sum(r["wer_after_alignment"] for r in successful) / len(successful)
        avg_wer_before = sum(r["wer_before_alignment"] for r in successful) / len(successful)

        lines.append("Averages across successful tracks:")
        lines.append(f"  MAE:               {avg_mae:.4f}s")
        lines.append(f"  Hit rate <0.1s:    {avg_hit:.1%}")
        lines.append(f"  WER before align:  {avg_wer_before:.1%}")
        lines.append(f"  WER after align:   {avg_wer_after:.1%}")
        lines.append("")
        lines.append("Interpretation:")
        lines.append("  MAE < 0.1s  = good timing (karaoke usable)")
        lines.append("  MAE < 0.05s = excellent timing")
        lines.append("  WER < 5%    = text is nearly correct")

    summary_path = RESULTS_DIR / "summary.txt"
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Summary saved to {summary_path}")
    print("\n" + "\n".join(lines))


if __name__ == "__main__":
    main()
