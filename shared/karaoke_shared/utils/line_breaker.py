"""Automatic line-break detection for syllable timings.

When LRC data is not available (e.g. online Sonoix flow), we need to
determine where to place ``\\n`` line-break markers in the syllable stream.

Two modes are supported:

- **Gap mode** (default for normal songs): breaks at large timing gaps
  between syllables, with a dynamic threshold adapted to the track.
- **Beat mode** (for rap / fast flow): uses ``librosa.beat.beat_track``
  on the vocal audio to find bar boundaries and break lines accordingly.

The function :func:`detect_line_breaks` auto-selects the mode based on
the gap distribution and injects ``\\n`` prefixes in-place.
"""

from __future__ import annotations

import time

import numpy as np
import structlog

from karaoke_shared.models.track import SyllableTiming

logger = structlog.get_logger(__name__)


def detect_line_breaks(
    timings: list[SyllableTiming],
    vocal_path: str | None = None,
) -> list[SyllableTiming]:
    """Inject ``\\n`` line-break markers into syllable timings.

    Analyses the gap distribution between consecutive syllables to decide
    between *gap mode* (normal songs with pauses) and *beat mode* (rap
    with minimal pauses).  Returns a new list with ``\\n`` prefixes
    injected at detected break points.

    Already-marked timings (those starting with ``\\n``) are returned
    unchanged.

    Args:
        timings: Syllable timings from transcription/syllabification.
        vocal_path: Optional path to vocal audio — used for beat detection
            in rap mode.  When ``None``, beat mode falls back to relaxed
            gap mode.

    Returns:
        New list of ``SyllableTiming`` with ``\\n`` prefixes at line breaks.
    """
    if len(timings) < 2:
        return list(timings)

    # If timings already contain \n markers (from LRC), return as-is.
    if any(s.syllable.startswith("\n") for s in timings):
        return list(timings)

    logger.info("line_break_detection_starting", syllables=len(timings))
    t0 = time.monotonic()

    # Analyse inter-syllable gaps.
    gaps = [timings[i].start - timings[i - 1].end for i in range(1, len(timings))]
    large_gap_count = sum(1 for g in gaps if g > 0.4)

    if large_gap_count >= 5:
        break_indices = _gap_mode(timings, gaps)
    elif vocal_path:
        break_indices = _beat_mode(timings, vocal_path)
    else:
        # Fallback: relaxed gap mode.
        break_indices = _gap_mode(timings, gaps, threshold_floor=0.2)

    result = _inject_breaks(timings, break_indices)

    logger.info(
        "line_break_detection_completed",
        breaks=len(break_indices),
        duration_sec=round(time.monotonic() - t0, 2),
    )

    return result


def _gap_mode(
    timings: list[SyllableTiming],
    gaps: list[float],
    threshold_floor: float = 0.3,
) -> list[int]:
    """Find line-break indices from timing gaps.

    The threshold is dynamic: ``max(threshold_floor, P75 * 2.5)`` so it
    adapts to tracks with different pacing.  Additionally, lines longer
    than 50 characters are force-broken at the next word boundary.
    """
    p75 = float(np.percentile(gaps, 75))
    threshold = max(threshold_floor, p75 * 2.5)

    break_indices: list[int] = []
    char_count = len(timings[0].syllable)

    for i in range(1, len(timings)):
        gap = gaps[i - 1]
        syl = timings[i].syllable
        is_word = syl.startswith(" ")

        # Break on large gap at word boundary.
        if gap > threshold and is_word:
            break_indices.append(i)
            char_count = len(syl)
            continue

        # Force-break long lines at word boundary.
        if char_count > 50 and is_word:
            break_indices.append(i)
            char_count = len(syl)
            continue

        char_count += len(syl)

    return break_indices


def _beat_mode(
    timings: list[SyllableTiming],
    vocal_path: str,
) -> list[int]:
    """Find line-break indices from beat structure (for rap).

    Uses ``librosa.beat.beat_track`` to detect beats in the vocal track,
    groups them into 4-beat bars (standard 4/4 meter), and places line
    breaks at bar boundaries aligned to the nearest word boundary.

    Falls back to relaxed gap mode if beat detection fails or finds too
    few beats.
    """
    import librosa  # noqa: PLC0415

    y, sr = librosa.load(vocal_path, sr=22050)
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    beat_times = librosa.frames_to_time(beat_frames, sr=sr)

    if len(beat_times) < 4:
        # Not enough beats — fallback to relaxed gap mode.
        gaps = [timings[i].start - timings[i - 1].end for i in range(1, len(timings))]
        return _gap_mode(timings, gaps, threshold_floor=0.2)

    # Group into 4-beat bars.
    bar_times = beat_times[::4]

    # Match bar boundaries to syllable positions.
    break_indices: list[int] = []
    bar_idx = 1  # skip first bar (start of song)

    for i in range(1, len(timings)):
        if bar_idx >= len(bar_times):
            break
        syl = timings[i]
        is_word = syl.syllable.startswith(" ")
        if is_word and syl.start >= bar_times[bar_idx] - 0.3:
            break_indices.append(i)
            bar_idx += 1

    return break_indices


def _inject_breaks(
    timings: list[SyllableTiming],
    break_indices: list[int],
) -> list[SyllableTiming]:
    """Inject ``\\n`` prefix at break positions.

    For syllables that start with a space (word boundary), the space is
    replaced with ``\\n``.  Otherwise ``\\n`` is prepended.
    """
    result = list(timings)
    for i in break_indices:
        syl = result[i]
        text = syl.syllable
        if text.startswith(" "):
            text = "\n" + text[1:]
        elif not text.startswith("\n"):
            text = "\n" + text
        result[i] = SyllableTiming(syllable=text, start=syl.start, end=syl.end)
    return result
