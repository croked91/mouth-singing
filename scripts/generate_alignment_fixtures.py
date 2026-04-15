"""Generate TorchCTCAligner test fixtures from an MP3 + lyrics.

Runs the full production pipeline (UVR → BackVocalSeparator → Silero
pre-trim → MMS forward + forced_align + merge_tokens) and dumps:

  * ``vocals.wav`` — 16 kHz mono lead-vocals, post-Silero-trim. This is
    exactly the waveform the adjustment methods see.
  * ``alignment.json`` — everything else needed to re-run the adjustment
    methods deterministically: ratio, trim_offset, words, first_flags,
    and the per-word token spans.

Intended to be executed inside the worker GPU container where the UVR /
Silero / MMS models are already cached::

    docker cp <mp3> worker:/tmp/leps.mp3
    docker cp <lyrics.txt> worker:/tmp/leps.txt
    docker compose ... exec -T worker python /project/scripts/generate_alignment_fixtures.py \\
        --mp3 /tmp/leps.mp3 --lyrics /tmp/leps.txt --output /tmp/fixtures/leps
    docker cp worker:/tmp/fixtures/leps ./tests/worker/fixtures/alignment/
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mp3", required=True)
    ap.add_argument("--lyrics", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--language", default="ru")
    args = ap.parse_args()

    import soundfile as sf

    from worker.app.config import settings
    from worker.gpu.back_vocal_separator import BackVocalSeparator
    from worker.gpu.torch_ctc_aligner import TorchCTCAligner, _SAMPLE_RATE
    from worker.gpu.uvr_separator import UVRSeparator

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    lyrics = Path(args.lyrics).read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # UVR separator (full vocals).
    # ------------------------------------------------------------------
    uvr = UVRSeparator(
        model_cache_dir=settings.model_cache_dir,
        media_root=settings.media_root,
        model_name=settings.uvr_model_name,
        chunk_batch_size=settings.uvr_chunk_batch_size,
        use_autocast=settings.uvr_use_autocast,
        overlap=settings.uvr_overlap,
    )
    vocals_path, _ = uvr.separate(args.mp3)
    uvr.cleanup()
    print(f"[uvr] vocals -> {vocals_path}")

    # ------------------------------------------------------------------
    # Back-vocal separator (lead-only vocals).
    # ------------------------------------------------------------------
    if settings.back_vocal_enabled:
        bv = BackVocalSeparator(
            model_cache_dir=settings.model_cache_dir,
            media_root=settings.media_root,
            model_name=settings.back_vocal_model_name,
            torch_device=settings.back_vocal_torch_device,
            chunk_batch_size=settings.back_vocal_chunk_batch_size,
            use_autocast=settings.back_vocal_use_autocast,
            overlap=settings.back_vocal_overlap,
        )
        lead_path, _ = bv.separate(vocals_path)
        bv.cleanup()
        print(f"[back_vocal] lead -> {lead_path}")
    else:
        lead_path = vocals_path

    # ------------------------------------------------------------------
    # MMS forward + forced_align + merge_tokens, reusing the aligner's
    # own internal helpers for exact fidelity with the production path.
    # ------------------------------------------------------------------
    aligner = TorchCTCAligner(
        device="cuda",
        model_cache_dir=settings.model_cache_dir,
        pre_trim_enabled=settings.mms_pre_trim_enabled,
        pre_trim_threshold=settings.mms_pre_trim_threshold,
        pre_trim_min_speech_ms=settings.mms_pre_trim_min_speech_ms,
        pre_trim_lead_in_ms=settings.mms_pre_trim_lead_in_ms,
        line_start_rms_adjust=settings.mms_line_start_rms_adjust,
        word_end_drift_adjust=settings.mms_word_end_drift_adjust,
        word_end_sustain_extend=settings.mms_word_end_sustain_extend,
    )
    aligner._ensure_model()

    pretrim_waveform = aligner._load_audio(lead_path)

    # Split Silero into its two phases so we can save the pre-refine
    # offset alongside the refined one. Mirrors _silero_trim_start.
    trim_offset = 0.0
    silero_start_samples = 0
    if aligner._pre_trim_enabled:
        aligner._ensure_silero()
        audio_cpu = pretrim_waveform.squeeze(0).cpu()
        ts = aligner._silero_get_ts(
            audio_cpu,
            aligner._silero_model,
            threshold=aligner._pre_trim_threshold,
            sampling_rate=_SAMPLE_RATE,
            min_speech_duration_ms=aligner._pre_trim_min_speech_ms,
            min_silence_duration_ms=500,
            speech_pad_ms=50,
        )
        if ts:
            silero_start_samples = int(ts[0]["start"])
            trim_offset = aligner._refine_silero_onset(
                audio_cpu.numpy(), silero_start_samples,
            )
    print(
        f"[silero] raw_start_samples={silero_start_samples} "
        f"refined_onset={trim_offset:.3f}s"
    )

    # Now produce the post-trim waveform for MMS, same as production.
    if trim_offset > 0.0:
        trim_samples = int(trim_offset * _SAMPLE_RATE)
        waveform = pretrim_waveform[:, trim_samples:]
    else:
        waveform = pretrim_waveform

    emission, ratio = aligner._forward_pass(waveform)
    words, transcript, first_flags = aligner._tokenize_lyrics(lyrics, args.language)
    word_spans = aligner._align_tokens(emission, transcript)
    print(f"[mms] ratio={ratio:.6f}, words={len(words)}, spans={len(word_spans)}")

    # ------------------------------------------------------------------
    # Serialize: vocals.wav (pre-trim — tests slice as needed) + alignment.json.
    # ------------------------------------------------------------------
    pretrim_np = pretrim_waveform.squeeze(0).cpu().numpy()
    sf.write(str(out / "vocals.wav"), pretrim_np, _SAMPLE_RATE, subtype="PCM_16")

    dump = {
        "ratio": float(ratio),
        "trim_offset": float(trim_offset),
        "silero_start_samples": int(silero_start_samples),
        "silero_threshold": float(aligner._pre_trim_threshold),
        "sample_rate": _SAMPLE_RATE,
        "language": args.language,
        "words": words,
        "first_flags": list(first_flags),
        "word_spans": [
            [
                {
                    "start": int(s.start),
                    "end": int(s.end),
                    "token": int(s.token),
                    "score": float(s.score),
                }
                for s in spans
            ]
            for spans in word_spans
        ],
    }
    (out / "alignment.json").write_text(
        json.dumps(dump, ensure_ascii=False, indent=2),
    )
    aligner.cleanup()

    print(f"[done] fixtures -> {out}")


if __name__ == "__main__":
    main()
