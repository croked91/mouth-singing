"""Variant CTC experiment (char-level): CTC Forced Aligner -> syllable timings.

Pipeline per track:
  1. Load audio with ctc_forced_aligner.load_audio (numpy, 16kHz mono)
  2. Run generate_emissions (ONNX model)
  3. Preprocess known lyrics with split_size='char' (romanize for Cyrillic)
  4. Run get_alignments + get_spans + postprocess_results -> char-level timings
  5. Reassemble syllable timings by consuming N chars per syllable (pyphen)

Unlike the word-level variant, syllable boundaries here come directly from
the audio — no proportional interpolation within words.  This should improve
accuracy for multi-syllable words.

Results saved to m3_test/variant_ctc/results/results_char.json and
summary_char.txt (word-level results are not overwritten).
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
# Step 1-4 — CTC Forced Alignment (char-level)
# ---------------------------------------------------------------------------


def run_ctc_char_alignment(
    audio_waveform,
    lyrics_text: str,
    language: str,
    alignment_model,
    alignment_tokenizer,
) -> list[dict]:
    """Run CTC forced alignment at character level and return char timings.

    Uses split_size='char'.  For Cyrillic text, romanize=True converts
    characters to Latin before alignment — the returned dicts will contain
    romanized (Latin) characters in the "text" field.  We consume entries
    purely by position count, so the actual text content does not matter
    for syllable assembly.

    Args:
        audio_waveform: 1D numpy float32 array from load_audio (16kHz).
        lyrics_text: Full lyrics with newlines replaced by spaces.
        language: Two-letter code ("ru", "en").
        alignment_model: ONNX InferenceSession (from AlignmentSingleton.model).
        alignment_tokenizer: Tokenizer (from AlignmentSingleton.tokenizer).

    Returns:
        List of {"text": str, "start": float, "end": float} dicts (seconds),
        one entry per non-blank character in the lyrics.
        Spaces / star entries that postprocess_results may return are included
        as-is; callers must filter them.
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

    print(
        f"    [CTC] Preprocessing text "
        f"(lang={lang_iso3}, romanize={romanize}, split_size=char) ..."
    )
    tokens_starred, text_starred = preprocess_text(
        lyrics_text,
        romanize=romanize,
        language=lang_iso3,
        split_size="char",
    )
    char_count = len([t for t in tokens_starred if t != "<star>"])
    print(f"    [CTC] Character token count: {char_count}")

    segments, scores, blank_token = get_alignments(
        emissions, tokens_starred, alignment_tokenizer
    )
    spans = get_spans(tokens_starred, segments, blank_token)
    # postprocess_results skips <star> entries and returns one dict per char.
    char_timestamps = postprocess_results(text_starred, spans, stride, scores)

    print(f"    [CTC] Got {len(char_timestamps)} char-level timings.")
    if char_timestamps:
        print(
            f"    [CTC] First 5 char entries: "
            f"{[e['text'] for e in char_timestamps[:5]]}"
        )
    return char_timestamps


# ---------------------------------------------------------------------------
# Step 5 — Build syllable timings from char-level timings
# ---------------------------------------------------------------------------


def _filter_content_chars(char_timestamps: list[dict]) -> list[dict]:
    """Remove whitespace-only and empty entries from char_timestamps.

    The CTC aligner may include space characters between words.  We only
    want the content characters so that positional counting lines up with
    the actual letter count in each syllable.

    Args:
        char_timestamps: Raw output from postprocess_results with split_size=char.

    Returns:
        Filtered list containing only entries whose "text" is a non-whitespace
        non-empty string.
    """
    return [
        entry for entry in char_timestamps
        if entry["text"].strip()
    ]


def build_syllable_timings_from_chars(
    char_timestamps: list[dict],
    lyrics_text: str,
    syllabifier: Syllabifier,
    language: str,
) -> list[SyllableTiming]:
    """Assemble syllable timings by consuming char_timestamps N chars at a time.

    Algorithm:
    1. Filter char_timestamps to content characters only.
    2. Parse lyrics_text into lines -> words.
    3. For each word, call syllabifier._split_word to get syllable parts.
    4. For each syllable part of length N, consume the next N entries from
       the filtered char list:
         - start = start of first consumed char
         - end   = end of last consumed char
    5. Assign display prefixes:
         - '' for the very first syllable overall
         - '\\n' for the first syllable of a new line
         - ' ' for the first syllable of any other new word
         - '' for continuation syllables within a word

    Positional mismatch handling: if the char_timestamps list is exhausted
    before all syllables are processed, remaining syllables are dropped with
    a warning.  If there are leftover chars, they are silently ignored.

    Args:
        char_timestamps: Raw output of run_ctc_char_alignment.
        lyrics_text: Full lyrics with \\n-separated lines (original newlines).
        syllabifier: Syllabifier instance for pyphen-based syllabification.
        language: Two-letter language code ("ru", "en").

    Returns:
        Flat list of SyllableTiming objects.
    """
    content_chars = _filter_content_chars(char_timestamps)
    char_cursor = 0  # index into content_chars
    total_chars = len(content_chars)

    all_timings: list[SyllableTiming] = []
    is_first_syllable_overall = True

    for line_idx, line in enumerate(lyrics_text.splitlines()):
        words = line.split()
        if not words:
            continue

        for word_idx, word in enumerate(words):
            parts = syllabifier._split_word(word, language)  # noqa: SLF001
            if not parts:
                continue

            # Determine the display prefix for the first syllable of this word.
            if is_first_syllable_overall:
                first_prefix = ""
            elif word_idx == 0:
                # First word of a new line (line_idx > 0 implied by the
                # is_first_syllable_overall check above).
                first_prefix = "\n"
            else:
                first_prefix = " "

            for syl_idx, part in enumerate(parts):
                # Number of content characters this syllable consumes.
                # Use the stripped length so punctuation-only suffixes/prefixes
                # (which have no letter content) don't add extra char slots.
                # However, fall back to 1 to avoid an infinite zero-width loop.
                n_chars = max(len(re.sub(r"[^\w]", "", part, flags=re.UNICODE)), 1)

                if char_cursor + n_chars > total_chars:
                    remaining = total_chars - char_cursor
                    print(
                        f"    [Map] Warning: ran out of char_timestamps at "
                        f"word='{word}' syllable='{part}' "
                        f"(need {n_chars}, have {remaining}). "
                        f"Stopping early."
                    )
                    return all_timings

                consumed = content_chars[char_cursor : char_cursor + n_chars]
                char_cursor += n_chars

                syl_start = consumed[0]["start"]
                syl_end = consumed[-1]["end"]

                # Ensure end > start (CTC occasionally collapses timestamps).
                if syl_end <= syl_start:
                    syl_end = syl_start + 0.05

                if syl_idx == 0:
                    display = first_prefix + part
                else:
                    display = part

                all_timings.append(
                    SyllableTiming(syllable=display, start=syl_start, end=syl_end)
                )
                is_first_syllable_overall = False

    leftover = total_chars - char_cursor
    if leftover > 0:
        print(f"    [Map] Note: {leftover} unused char_timestamps entries.")

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
        predicted: SyllableTiming list from build_syllable_timings_from_chars.
        reference: List of {"syllable": str, "start": float, ...} dicts.
        label: Short name for display (e.g. "ctc_char").

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
    print(
        f"\n    {'Syllable':<12} | {'Ref start':>10} | "
        f"{'Pred start':>10} | {'Delta':>8}"
    )
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
    """Run the CTC char-level alignment pipeline on a single track.

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
    char_count = len(lyrics_flat.replace(" ", ""))
    word_count = len(lyrics_flat.split())
    print(f"    Lyrics: {len(lyrics_flat)} chars total, {char_count} non-space, {word_count} words")

    # load_audio returns a 1D numpy float32 array resampled to 16kHz.
    print(f"    [CTC] Loading audio from {meta.vocals_path} ...")
    audio_waveform = load_audio(str(meta.vocals_path), ret_type="np")
    print(f"    [CTC] Audio waveform shape: {audio_waveform.shape}")

    # Run CTC forced alignment at character level.
    char_timestamps = run_ctc_char_alignment(
        audio_waveform,
        lyrics_flat,
        meta.language,
        alignment_model,
        alignment_tokenizer,
    )

    # Assemble syllable timings by consuming chars per syllable.
    syllable_timings = build_syllable_timings_from_chars(
        char_timestamps,
        lyrics_text,
        syllabifier,
        meta.language,
    )
    print(f"    [Map] Produced {len(syllable_timings)} syllable timings.")

    return {
        "timings": [t.as_dict() for t in syllable_timings],
        "syllable_count": len(syllable_timings),
        "char_timestamp_count": len(char_timestamps),
        "lyrics_char_count": char_count,
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
    """Write a human-readable summary table to results/summary_char.txt.

    Args:
        all_results: Dict keyed by track number string, values are per-track
            result dicts as stored in results_char.json.
    """
    lines: list[str] = []
    lines.append("Variant CTC Experiment Summary (char-level)")
    lines.append("=" * 75)
    lines.append("")
    lines.append(
        "Approach: CTC Forced Aligner (known text, char-level) -> "
        "pyphen syllable boundaries from char timestamps"
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

        metrics = result.get("ctc_char", {})
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

    summary_path = RESULTS_DIR / "summary_char.txt"
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nSummary saved to {summary_path}")
    print("\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the CTC char-level forced alignment experiment for all 5 test tracks."""
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
        metrics = evaluate_timings(predicted_timings, reference, "ctc_char")

        all_results[str(track_num)] = {
            "meta": {
                "artist": meta.artist,
                "title": meta.title,
                "language": meta.language,
            },
            "syllable_count": output["syllable_count"],
            "char_timestamp_count": output["char_timestamp_count"],
            "lyrics_char_count": output["lyrics_char_count"],
            "lyrics_word_count": output["lyrics_word_count"],
            "ctc_char": metrics,
            "ctc_char_timings": output["timings"],
        }

    # Persist full results.
    results_path = RESULTS_DIR / "results_char.json"
    results_path.write_text(
        json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nResults saved to {results_path}")

    _write_summary(all_results)


if __name__ == "__main__":
    main()
