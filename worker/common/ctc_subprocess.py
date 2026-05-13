"""Standalone CTC alignment script run via subprocess.Popen.

Usage:
    python -m worker.common.ctc_subprocess \
        --vocals /path/to/vocals.mp3 \
        --language ru \
        --batch-size 16 \
        --output /tmp/result.json \
        <<< "lyrics text here"

Writes JSON result to --output: {"timings": [...], "stats": {...}}
On error, writes: {"error": "message"}
"""

from __future__ import annotations

import argparse
import json
import sys


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vocals", required=True)
    parser.add_argument("--lyrics-file", required=True)
    parser.add_argument("--language", required=True)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    try:
        with open(args.lyrics_file, "r", encoding="utf-8") as f:
            lyrics_text = f.read()

        import onnxruntime
        from ctc_forced_aligner import (
            AlignmentSingleton,
            Tokenizer,
            generate_emissions,
            get_alignments,
            get_spans,
            load_audio,
            postprocess_results,
            preprocess_text,
        )

        from karaoke_shared.utils.syllabifier import Syllabifier

        # --- constants ---
        LANG_ISO3 = {"ru": "rus", "en": "eng"}

        def lang_flags(language):
            l = LANG_ISO3.get(language, "eng")
            return l, language != "en"

        def time_to_frame(t, stride):
            return int(t * 1000 / stride)

        # --- load model ---
        aligner = AlignmentSingleton()
        tokenizer = Tokenizer()
        sess_opts = onnxruntime.SessionOptions()
        sess_opts.intra_op_num_threads = 2
        sess_opts.inter_op_num_threads = 1
        model = onnxruntime.InferenceSession(
            aligner.model_path,
            sess_options=sess_opts,
            providers=["CPUExecutionProvider"],
        )

        syllabifier = Syllabifier()

        # --- emissions ---
        waveform = load_audio(args.vocals, ret_type="np")
        emissions, stride_ms = generate_emissions(
            model, waveform, batch_size=args.batch_size
        )

        # --- guard: truncate lyrics if too many words for available frames ---
        # CTC requires n_frames >= n_tokens.  Each word → ~2 tokens (word + star).
        # Allow at most n_frames // 2 words to prevent OOM.
        # CTC get_alignments allocates O(frames × tokens) memory.
        # With romanized Russian, each word ≈ 2 tokens (word + star).
        # Cap at 500 words to stay well within memory limits.
        max_words = min(500, emissions.shape[0] // 4)
        lyrics_lines = lyrics_text.splitlines()
        truncated_lines = []
        word_count = 0
        for line in lyrics_lines:
            line_words = len(line.split())
            if word_count + line_words > max_words:
                break
            truncated_lines.append(line)
            word_count += line_words
        lyrics_text = "\n".join(truncated_lines)

        # --- word-level alignment ---
        lyrics_flat = lyrics_text.replace("\n", " ").strip()
        lang_iso3, romanize = lang_flags(args.language)

        tokens_starred, text_starred = preprocess_text(
            lyrics_flat, romanize=romanize, language=lang_iso3, split_size="word",
        )
        segments, scores, blank_token = get_alignments(
            emissions, tokens_starred, tokenizer
        )
        spans = get_spans(tokens_starred, segments, blank_token)
        word_timestamps = postprocess_results(text_starred, spans, stride_ms, scores)

        # --- build lyrics words ---
        lyrics_words = []
        for line in lyrics_text.splitlines():
            words = line.split()
            if not words:
                continue
            for idx, word in enumerate(words):
                lyrics_words.append((word, idx == 0))

        ctc_count = len(word_timestamps)
        lyrics_count = len(lyrics_words)
        match_count = min(ctc_count, lyrics_count)
        total_frames = emissions.shape[0]

        stats = {"total_words": match_count, "proportional_fallback": 0}
        all_timings = []
        is_first_overall = True

        for i in range(match_count):
            we = word_timestamps[i]
            lw, is_first_in_line = lyrics_words[i]
            ws = we["start"]
            wend = we["end"]
            if wend <= ws:
                wend = ws + 0.05

            if is_first_overall:
                prefix = ""
            elif is_first_in_line:
                prefix = "\n"
            else:
                prefix = " "

            fs = time_to_frame(ws, stride_ms)
            fe = time_to_frame(wend, stride_ms)
            fs = max(0, min(fs, total_frames - 1))
            fe = max(fs + 1, min(fe, total_frames))
            nf = fe - fs

            # NOTE: char-level CTC (get_alignments on emission slices)
            # is disabled — repeated calls cause heap corruption in ONNX Runtime.
            # Word-level boundaries + proportional syllable split is used instead.

            # Proportional fallback
            parts = syllabifier._split_word(lw, args.language)
            if not parts:
                is_first_overall = False
                continue
            duration = wend - ws
            if len(parts) == 1:
                all_timings.append({"syllable": prefix + parts[0], "start": ws, "end": wend})
            else:
                cl = [max(len(p.strip()), 1) for p in parts]
                tc = sum(cl)
                cur = ws
                for pi, part in enumerate(parts):
                    frac = cl[pi] / tc
                    send = cur + duration * frac
                    d = prefix + part if pi == 0 else part
                    all_timings.append({"syllable": d, "start": round(cur, 3), "end": round(send, 3)})
                    cur = send
            stats["proportional_fallback"] += 1
            is_first_overall = False

        result = {"timings": all_timings, "stats": stats}
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)

    except Exception as exc:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump({"error": str(exc)}, f, ensure_ascii=False)
        sys.exit(1)


if __name__ == "__main__":
    main()
