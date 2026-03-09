"""Variant CTC experiment: CTC Forced Aligner -> syllable-level timings.

Pipeline per track:
  1. Load audio with ctc_forced_aligner.load_audio (numpy, 16kHz mono)
  2. Run generate_emissions (ONNX model)
  3. Preprocess known lyrics with split_size='word' (romanize for Cyrillic)
  4. Run get_alignments + get_spans + postprocess_results -> word-level timings
  5. Map word timings to syllable timings using pyphen (proportional by char count)

The CTC aligner receives the correct lyrics text, so no ASR is needed.
The word-level output is then syllabified in the same way the existing
Syllabifier._from_word_tokens() does — proportional split within each word.

This gives us a clean lower bound: how well can CTC align words when the
text is known, and does pyphen proportional split give acceptable syllable
accuracy on top of that?

Results saved to m3_test/variant_ctc/results/.
"""

from __future__ import annotations

import difflib
import json
import re
import sys
from pathlib import Path
from typing import NamedTuple

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
# Step 1-4 — CTC Forced Alignment (word-level)
# ---------------------------------------------------------------------------


def run_ctc_word_alignment(
    audio_waveform,
    lyrics_text: str,
    language: str,
    alignment_model,
    alignment_tokenizer,
) -> list[dict]:
    """Run CTC forced alignment at word level and return word timings.

    Uses split_size='word' (the default).  For Cyrillic text, romanize=True
    converts characters to Latin before alignment — the returned dict's "text"
    field contains the original (non-romanized) word from text_starred.

    Args:
        audio_waveform: 1D numpy float32 array from load_audio (16kHz).
        lyrics_text: Full lyrics with newlines replaced by spaces.
        language: Two-letter code ("ru", "en").
        alignment_model: ONNX InferenceSession (from AlignmentSingleton.model).
        alignment_tokenizer: Tokenizer (from AlignmentSingleton.tokenizer).

    Returns:
        List of {"text": str, "start": float, "end": float} dicts (seconds).
        <star> entries are already filtered out by postprocess_results.
    """
    from ctc_forced_aligner import (
        generate_emissions,
        get_alignments,
        get_spans,
        postprocess_results,
        preprocess_text,
    )

    lang_iso3 = _LANG_ISO3.get(language, "eng")
    # romanize=True converts Cyrillic to Latin before CTC alignment.
    # The model's vocabulary only covers Latin/ASCII characters.
    romanize = language != "en"

    print(f"    [CTC] Generating emissions (audio shape: {audio_waveform.shape}) ...")
    emissions, stride = generate_emissions(
        alignment_model, audio_waveform, batch_size=16
    )
    print(f"    [CTC] Emissions shape: {emissions.shape}, stride: {stride}ms")

    print(f"    [CTC] Preprocessing text "
          f"(lang={lang_iso3}, romanize={romanize}, split_size=word) ...")
    tokens_starred, text_starred = preprocess_text(
        lyrics_text,
        romanize=romanize,
        language=lang_iso3,
        split_size="word",
    )
    print(f"    [CTC] Word token count: {len([t for t in tokens_starred if t != '<star>'])}")

    segments, scores, blank_token = get_alignments(
        emissions, tokens_starred, alignment_tokenizer
    )
    spans = get_spans(tokens_starred, segments, blank_token)
    # postprocess_results skips <star> entries and returns one dict per word.
    word_timestamps = postprocess_results(text_starred, spans, stride, scores)

    print(f"    [CTC] Got {len(word_timestamps)} word-level timings.")
    return word_timestamps


# ---------------------------------------------------------------------------
# Step 5 — Map word timings to syllable timings
# ---------------------------------------------------------------------------


def _split_and_distribute(
    word_text: str,
    start_sec: float,
    end_sec: float,
    syllabifier: Syllabifier,
    language: str,
    is_first_word_in_line: bool,
    is_first_syllable_overall: bool,
) -> list[SyllableTiming]:
    """Split a word into syllables and distribute the word's time span.

    Each syllable gets a share of the word's duration proportional to its
    character count.  This is the same algorithm used by Syllabifier._from_word_tokens().

    Syllable display prefixes:
    - No prefix: first syllable of the very first word in the track.
    - '\\n' prefix: first syllable of the first word in a new line.
    - ' ' prefix: first syllable of any other new word.
    - No prefix: continuation syllables within a word.

    Args:
        word_text: The original word string (possibly with punctuation).
        start_sec: Word start time in seconds.
        end_sec: Word end time in seconds.
        syllabifier: Syllabifier instance.
        language: Two-letter language code.
        is_first_word_in_line: True if this is the first word of a new line.
        is_first_syllable_overall: True only for the very first word of the track.

    Returns:
        List of SyllableTiming objects for this word's syllables.
    """
    duration = end_sec - start_sec
    parts = syllabifier._split_word(word_text, language)  # noqa: SLF001
    if not parts:
        return []

    # Determine the display prefix for the first syllable of this word.
    if is_first_syllable_overall:
        first_prefix = ""
    elif is_first_word_in_line:
        first_prefix = "\n"
    else:
        first_prefix = " "

    if len(parts) == 1:
        display = first_prefix + parts[0]
        return [SyllableTiming(syllable=display, start=start_sec, end=end_sec)]

    # Proportional split by character count (strip punctuation for counting).
    char_lengths = [max(len(p.strip()), 1) for p in parts]
    total_chars = sum(char_lengths)

    timings: list[SyllableTiming] = []
    cursor = start_sec

    for i, part in enumerate(parts):
        fraction = char_lengths[i] / total_chars
        syl_end = cursor + duration * fraction

        if i == 0:
            display = first_prefix + part
        else:
            display = part

        timings.append(SyllableTiming(
            syllable=display, start=cursor, end=syl_end
        ))
        cursor = syl_end

    return timings


def build_syllable_timings(
    word_timestamps: list[dict],
    lyrics_text: str,
    syllabifier: Syllabifier,
    language: str,
) -> list[SyllableTiming]:
    """Map CTC word-level timings to syllable-level timings.

    We use the original lyrics line structure to assign the correct '\\n'
    prefix to the first syllable of each new line.  The CTC word_timestamps
    are matched to the lyrics words positionally — we assume the aligner
    returns words in order (it always does for forced alignment).

    Mapping algorithm:
    1. Parse lyrics into lines, then words within each line.
    2. Walk word_timestamps in parallel with the lyrics word list.
    3. For each word, call _split_and_distribute to get syllable timings.
    4. Track whether the current word is the first in its line.

    Note: If the number of CTC words doesn't match the lyrics word count,
    we align by position up to min(len(word_timestamps), total_lyrics_words).

    Args:
        word_timestamps: Word-level output from run_ctc_word_alignment.
        lyrics_text: Full lyrics with \\n-separated lines.
        syllabifier: Syllabifier instance.
        language: Two-letter language code.

    Returns:
        Flat list of SyllableTiming objects.
    """
    # Build a flat list of (word_text, is_first_in_line) pairs from lyrics.
    lyrics_words: list[tuple[str, bool]] = []
    for line in lyrics_text.splitlines():
        words = line.split()
        if not words:
            continue
        for word_idx, word in enumerate(words):
            lyrics_words.append((word, word_idx == 0))

    ctc_word_count = len(word_timestamps)
    lyrics_word_count = len(lyrics_words)

    print(f"    [Map] CTC words: {ctc_word_count}, "
          f"lyrics words: {lyrics_word_count}")

    if ctc_word_count != lyrics_word_count:
        print(f"    [Map] Warning: word count mismatch — "
              f"matching first {min(ctc_word_count, lyrics_word_count)} words.")

    match_count = min(ctc_word_count, lyrics_word_count)
    all_timings: list[SyllableTiming] = []

    for i in range(match_count):
        word_entry = word_timestamps[i]
        lyrics_word, is_first_in_line = lyrics_words[i]

        start_sec = word_entry["start"]
        end_sec = word_entry["end"]

        # Ensure end > start (the aligner occasionally collapses them).
        if end_sec <= start_sec:
            end_sec = start_sec + 0.05

        syl_timings = _split_and_distribute(
            word_text=lyrics_word,
            start_sec=start_sec,
            end_sec=end_sec,
            syllabifier=syllabifier,
            language=language,
            is_first_word_in_line=is_first_in_line,
            is_first_syllable_overall=(i == 0),
        )
        all_timings.extend(syl_timings)

    return all_timings


# ---------------------------------------------------------------------------
# Evaluation
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
        predicted: SyllableTiming list from build_syllable_timings.
        reference: List of {"syllable": str, "start": float, ...} dicts.
        label: Short name for display (e.g. "ctc").

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

    # Print first 20 rows for visual inspection.
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
    """Run the CTC alignment pipeline on a single track.

    Args:
        meta: Track metadata and file paths.
        alignment_model: ONNX InferenceSession (shared across all tracks).
        alignment_tokenizer: Tokenizer paired with the model.
        syllabifier: Syllabifier instance (shared across all tracks).

    Returns:
        Dict with "timings" (list of serialisable dicts) and stats.
    """
    from ctc_forced_aligner import load_audio

    print(f"\n  --- Track {meta.track_num}: {meta.artist} - {meta.title} ---")

    lyrics_text = meta.lyrics_path.read_text(encoding="utf-8")
    # CTC aligner expects a single string; replace newlines with spaces.
    lyrics_flat = lyrics_text.replace("\n", " ").strip()
    word_count = len(lyrics_flat.split())
    print(f"    Lyrics: {len(lyrics_flat)} chars, {word_count} words")

    # load_audio returns a 1D numpy float32 array resampled to 16kHz.
    print(f"    [CTC] Loading audio from {meta.vocals_path} ...")
    audio_waveform = load_audio(str(meta.vocals_path), ret_type="np")
    print(f"    [CTC] Audio waveform shape: {audio_waveform.shape}")

    # Run CTC forced alignment at word level.
    word_timestamps = run_ctc_word_alignment(
        audio_waveform,
        lyrics_flat,
        meta.language,
        alignment_model,
        alignment_tokenizer,
    )

    # Map word timings to syllable timings via pyphen proportional split.
    syllable_timings = build_syllable_timings(
        word_timestamps,
        lyrics_text,
        syllabifier,
        meta.language,
    )
    print(f"    [Map] Produced {len(syllable_timings)} syllable timings.")

    return {
        "timings": [t.as_dict() for t in syllable_timings],
        "syllable_count": len(syllable_timings),
        "word_timestamp_count": len(word_timestamps),
        "lyrics_word_count": word_count,
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
    """Write a human-readable summary table to results/summary.txt.

    Args:
        all_results: Dict keyed by track number string, values are per-track
            result dicts as stored in results.json.
    """
    lines: list[str] = []
    lines.append("Variant CTC Experiment Summary")
    lines.append("=" * 75)
    lines.append("")
    lines.append(
        "Approach: CTC Forced Aligner (known text, word-level) -> "
        "pyphen proportional syllable split"
    )
    lines.append("")
    lines.append(
        f"{'Track':<6} | {'Artist/Title':<38} | {'Lang':>4} | "
        f"{'MAE':>8} | {'Hit%':>7} | {'Matched':>9}"
    )
    lines.append("-" * 80)

    all_maes: list[float] = []

    for track_num_str, result in all_results.items():
        if "error" in result and "meta" not in result:
            lines.append(f"{track_num_str:<6} | ERROR: {result['error']}")
            continue

        meta = result["meta"]
        short_title = f"{meta['artist']} - {meta['title']}"[:37]
        lang = meta["language"]

        metrics = result.get("ctc", {})
        mae = metrics.get("mae")
        hit = metrics.get("hit_rate_01s")
        matched = metrics.get("matched_count", 0)
        total = metrics.get("total_ref", 0)

        mae_str = f"{mae:.3f}s" if mae is not None else "N/A"
        hit_str = f"{hit:.1%}" if hit is not None else "N/A"
        matched_str = f"{matched}/{total}"

        lines.append(
            f"{track_num_str:<6} | {short_title:<38} | {lang:>4} | "
            f"{mae_str:>8} | {hit_str:>7} | {matched_str:>9}"
        )

        if mae is not None:
            all_maes.append(mae)

    lines.append("")
    if all_maes:
        avg_mae = sum(all_maes) / len(all_maes)
        lines.append(f"Average MAE: {avg_mae:.3f}s")
        good = sum(1 for m in all_maes if m < 0.15)
        lines.append(f"Tracks with MAE < 0.15s: {good}/{len(all_maes)}")

    summary_path = RESULTS_DIR / "summary.txt"
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nSummary saved to {summary_path}")
    print("\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the CTC forced alignment experiment for all 5 test tracks."""
    from ctc_forced_aligner import AlignmentSingleton

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # AlignmentSingleton downloads the ONNX model on first use and caches it
    # at ~/ctc_forced_aligner/model.onnx.  Reused for all tracks.
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
        metrics = evaluate_timings(predicted_timings, reference, "ctc")

        all_results[str(track_num)] = {
            "meta": {
                "artist": meta.artist,
                "title": meta.title,
                "language": meta.language,
            },
            "syllable_count": output["syllable_count"],
            "word_timestamp_count": output["word_timestamp_count"],
            "lyrics_word_count": output["lyrics_word_count"],
            "ctc": metrics,
            "ctc_timings": output["timings"],
        }

    # Persist full results.
    results_path = RESULTS_DIR / "results.json"
    results_path.write_text(
        json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nResults saved to {results_path}")

    _write_summary(all_results)


if __name__ == "__main__":
    main()
