#!/usr/bin/env python3
"""Smoke test: run individual pipeline components on a real track.

Usage (from v3-rc1/worker/ with conda bootstrap):
    PYTHONPATH="../shared:." python tools/smoke_test.py

Tests each component independently:
  1. VAD on vocals.wav
  2. Whisper ASR on cleaned vocals
  3. LyricsSearcher with OpenAI API
  4. CTCAligner on vocals + lyrics
  5. Full pipeline timing report
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Paths
RC1_ROOT = Path(__file__).resolve().parent.parent.parent
TEST_DATA = RC1_ROOT.parent / "m3_test" / "test_data" / "1"
KEYS_DIR = RC1_ROOT.parent / "keys"

# Ensure imports work
sys.path.insert(0, str(RC1_ROOT / "shared"))
sys.path.insert(0, str(RC1_ROOT / "worker"))


def banner(msg: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {msg}")
    print(f"{'='*60}")


def test_vad():
    banner("Step 1: VAD")
    from app.pipeline.vad_processor import VADProcessor

    vad = VADProcessor(top_db=35)
    vocals_path = str(TEST_DATA / "vocals.wav")

    t0 = time.time()
    result = vad.process(vocals_path)
    elapsed = time.time() - t0

    print(f"  Input:  {vocals_path}")
    print(f"  Output: {result}")
    print(f"  Time:   {elapsed:.1f}s")
    return result


def test_whisper(audio_path: str):
    banner("Step 2: Whisper ASR")
    from app.pipeline.whisper_transcriber import WhisperTranscriber

    device = "cuda" if _has_cuda() else "cpu"
    compute = "float16" if device == "cuda" else "int8"

    t0 = time.time()
    whisper = WhisperTranscriber(
        model_size="tiny", device=device, compute_type=compute,
    )
    load_time = time.time() - t0

    t0 = time.time()
    result = whisper.transcribe(audio_path)
    infer_time = time.time() - t0

    print(f"  Device:     {device}")
    print(f"  Load time:  {load_time:.1f}s")
    print(f"  Infer time: {infer_time:.1f}s")
    print(f"  Language:   {result.language}")
    print(f"  Confidence: {result.confidence:.3f}")
    print(f"  Text (first 200): {result.text[:200]}")

    whisper.cleanup()
    return result


def test_lyrics_search(asr_text: str, language: str):
    banner("Step 3: Lyrics Search (LLM identify + Genius fetch)")

    openai_key_path = KEYS_DIR / "open-ai-test-key.txt"
    genius_key_path = KEYS_DIR / "genius.txt"

    if not openai_key_path.exists() or not genius_key_path.exists():
        missing = []
        if not openai_key_path.exists():
            missing.append(str(openai_key_path))
        if not genius_key_path.exists():
            missing.append(str(genius_key_path))
        print(f"  SKIP: missing keys: {', '.join(missing)}")
        lyrics = (TEST_DATA / "lyrics.txt").read_text(encoding="utf-8")
        meta = json.loads((TEST_DATA / "meta.json").read_text())
        print(f"  Using test data lyrics ({len(lyrics)} chars)")
        return type("LR", (), {
            "artist": meta["artist"], "title": meta["title"],
            "lyrics": lyrics, "language": language,
            "confidence": "test", "source_note": "test_data",
        })()

    openai_key = openai_key_path.read_text().strip()
    # genius.txt has "token: XXX" format on line 3
    genius_lines = genius_key_path.read_text().strip().splitlines()
    genius_token = None
    for line in genius_lines:
        if line.lower().startswith("token:"):
            genius_token = line.split(":", 1)[1].strip()
            break
    if not genius_token:
        print(f"  ERROR: could not parse genius token from {genius_key_path}")
        return None

    from app.pipeline.lyrics_searcher import LyricsSearcher

    searcher = LyricsSearcher(
        openai_api_key=openai_key,
        genius_token=genius_token,
        model="gpt-4o-mini",
        timeout=60.0,
        max_retries=1,
    )

    meta = json.loads((TEST_DATA / "meta.json").read_text())

    import asyncio
    t0 = time.time()
    try:
        result = asyncio.run(searcher.search(
            asr_text=asr_text,
            detected_language=language,
            artist_hint=meta["artist"],
            title_hint=meta["title"],
        ))
    except Exception as exc:
        elapsed = time.time() - t0
        print(f"  API FAILED ({elapsed:.1f}s): {exc}")
        print(f"  Falling back to test data lyrics...")
        lyrics = (TEST_DATA / "lyrics.txt").read_text(encoding="utf-8")
        return type("LR", (), {
            "artist": meta["artist"], "title": meta["title"],
            "lyrics": lyrics, "language": language,
            "confidence": "fallback", "source_note": "test_data",
        })()

    elapsed = time.time() - t0

    print(f"  Time:       {elapsed:.1f}s")
    print(f"  Artist:     {result.artist}")
    print(f"  Title:      {result.title}")
    print(f"  Language:   {result.language}")
    print(f"  Confidence: {result.confidence}")
    print(f"  Source:     {result.source_note}")
    print(f"  Lyrics len: {len(result.lyrics)} chars")
    print(f"  First 300:  {result.lyrics[:300]}")
    return result


def test_ctc(vocals_path: str, lyrics: str, language: str):
    banner("Step 4: CTC Alignment")
    from app.pipeline.ctc_aligner import CTCAligner
    from karaoke_shared.utils.syllabifier import Syllabifier

    t0 = time.time()
    aligner = CTCAligner(syllabifier=Syllabifier())
    load_time = time.time() - t0

    t0 = time.time()
    timings, stats = aligner.align(vocals_path, lyrics, language)
    align_time = time.time() - t0

    print(f"  Model load: {load_time:.1f}s")
    print(f"  Align time: {align_time:.1f}s")
    print(f"  Words:      {stats.total_words}")
    print(f"  Char-level: {stats.char_level_used} ({stats.char_level_used/max(stats.total_words,1)*100:.0f}%)")
    print(f"  Fallback:   {stats.proportional_fallback}")
    print(f"  Syllables:  {len(timings)}")
    print(f"\n  First 20 syllable timings:")
    for t in timings[:20]:
        print(f"    [{t.start:7.3f} - {t.end:7.3f}] {t.syllable!r}")

    return timings, stats


def _has_cuda():
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


def main():
    banner("v3-rc1 Smoke Test")
    meta = json.loads((TEST_DATA / "meta.json").read_text())
    print(f"  Track: {meta['artist']} - {meta['title']}")
    print(f"  Language: {meta['language']}")
    print(f"  CUDA: {_has_cuda()}")

    total_t0 = time.time()

    # Step 1: VAD
    cleaned_path = test_vad()

    # Step 2: Whisper
    whisper_result = test_whisper(cleaned_path)

    # Step 3: Lyrics search
    lyrics_result = test_lyrics_search(whisper_result.text, whisper_result.language)

    # Step 4: CTC alignment (use original vocals, not cleaned)
    vocals_path = str(TEST_DATA / "vocals.wav")
    timings, stats = test_ctc(vocals_path, lyrics_result.lyrics, meta["language"])

    total_elapsed = time.time() - total_t0

    banner("Summary")
    print(f"  Total time:       {total_elapsed:.1f}s")
    print(f"  Syllable timings: {len(timings)}")
    print(f"  Char-level:       {stats.char_level_used}/{stats.total_words}")
    print(f"  PASSED")


if __name__ == "__main__":
    main()
