"""Build alignment segments from VAD intervals.

Utility for distributing lyrics lines across audio regions.
Currently unused by the main pipeline (single-pass CTC alignment),
but kept for potential future segmented alignment approaches.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)

_MERGE_GAP = 0.5
_MIN_SEGMENT_SEC = 1.0


def build_segments_from_vad(
    vad_segments: list[tuple[float, float]],
    lyrics_text: str,
) -> list[tuple[float, float, str]]:
    """Distribute lyrics lines across VAD-voiced intervals.

    Args:
        vad_segments: Voiced intervals as (start_sec, end_sec).
        lyrics_text: Full lyrics text (line-separated).

    Returns:
        List of (start_sec, end_sec, text) tuples — one per lyrics line.
    """
    lines = [ln for ln in lyrics_text.splitlines() if ln.strip()]
    if not lines or not vad_segments:
        return []

    merged = _merge_segments(sorted(vad_segments))
    if not merged:
        return []

    total_dur = sum(end - start for start, end in merged)
    if total_dur <= 0:
        return []

    result: list[tuple[float, float, str]] = []
    line_idx = 0

    for reg_start, reg_end in merged:
        if line_idx >= len(lines):
            break

        reg_dur = reg_end - reg_start
        frac = reg_dur / total_dur
        n_lines = max(1, round(frac * len(lines)))
        n_lines = min(n_lines, len(lines) - line_idx)

        seg_lines = lines[line_idx : line_idx + n_lines]
        line_idx += n_lines

        result.extend(_split_interval_by_lines(reg_start, reg_end, seg_lines))

    if line_idx < len(lines) and result:
        last_start, last_end, last_text = result[-1]
        result.pop()
        remaining = [last_text] + lines[line_idx:]
        result.extend(_split_interval_by_lines(last_start, last_end, remaining))

    return result


def _split_interval_by_lines(
    start: float,
    end: float,
    lines: list[str],
) -> list[tuple[float, float, str]]:
    """Split a time interval into sub-segments, one per lyrics line."""
    if not lines:
        return []
    if len(lines) == 1:
        return [(start, end, lines[0])]

    word_counts = [max(len(ln.split()), 1) for ln in lines]
    total_words = sum(word_counts)
    duration = end - start

    result: list[tuple[float, float, str]] = []
    cur = start
    for i, line in enumerate(lines):
        frac = word_counts[i] / total_words
        sub_end = cur + duration * frac if i < len(lines) - 1 else end
        result.append((cur, sub_end, line))
        cur = sub_end

    return result


def _merge_segments(
    segments: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Merge close segments and enforce minimum duration."""
    if not segments:
        return []

    merged: list[tuple[float, float]] = [segments[0]]

    for start, end in segments[1:]:
        prev_start, prev_end = merged[-1]
        if start - prev_end < _MERGE_GAP:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))

    result: list[tuple[float, float]] = []
    for start, end in merged:
        if result and (end - start) < _MIN_SEGMENT_SEC:
            prev_start, prev_end = result[-1]
            result[-1] = (prev_start, max(prev_end, end))
        else:
            result.append((start, end))

    return result
