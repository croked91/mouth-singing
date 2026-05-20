"""Automatic local/global realign for alignment draft generation.

This module implements a conservative search pass intended to reduce manual
editor work while keeping safety guarantees:
- manual-lane gating for known bad sources
- line-level hypothesis generation (shift/merge/split/drop/duplicate)
- beam-searched global path under hard timing constraints
- draft-only result with explainable diagnostics
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from time import monotonic

from karaoke_shared.models.alignment import (
    AlignmentDocument,
    AlignmentLine,
    AlignmentSyllable,
    AlignmentWord,
)

_MIN_SYLLABLE = 0.03
_MAX_SYLLABLE = 2.5
_MAX_LINE_DENSITY = 8.0
_DEFAULT_GAP = 0.04

# Start at 0.8s and widen progressively.
_WINDOW_PADDINGS = (0.8, 1.2, 1.6, 2.4)

_MAX_CANDIDATES_PER_LINE = 24
_BEAM_WIDTH = 16
_MAX_TIME_PER_TRACK_SEC = 600.0
_MAX_TIME_PER_BLOCK_SEC = 600.0

_ACCEPT_DELTA_SCORE = 0.10
_HIGH_CONFIDENCE = 0.78
_MEDIUM_CONFIDENCE = 0.62


@dataclass
class RealignResult:
    document: AlignmentDocument
    diagnostics: dict


@dataclass
class Candidate:
    line_id: str
    kind: str
    line: AlignmentLine
    words: list[AlignmentWord]
    syllables: list[AlignmentSyllable]
    local_score: float
    penalties: dict[str, float]


def _line_flags(document: AlignmentDocument, line_id: str) -> list[str]:
    line = next((x for x in document.lines if x.id == line_id), None)
    if line is None:
        return []
    flags: list[str] = []
    if line.end <= line.start:
        flags.append("negative_duration")

    syllables = [s for s in document.syllables if s.line_id == line.id]
    syllables.sort(key=lambda s: (s.start, s.end))

    if any((s.end - s.start) < _MIN_SYLLABLE for s in syllables):
        flags.append("too_short_syllable")
    if any((s.end - s.start) > _MAX_SYLLABLE for s in syllables):
        flags.append("too_long_syllable")

    for idx in range(1, len(syllables)):
        if syllables[idx].start < syllables[idx - 1].end:
            flags.append("overlap")
            break

    line_duration = max(line.end - line.start, 0.001)
    if (len(syllables) / line_duration) > _MAX_LINE_DENSITY:
        flags.append("line_too_dense")
    return flags


def _baseline_score(document: AlignmentDocument) -> float:
    penalty = 0.0
    for line in document.lines:
        for flag in _line_flags(document, line.id):
            penalty += {
                "negative_duration": 20.0,
                "overlap": 12.0,
                "too_short_syllable": 5.0,
                "too_long_syllable": 5.0,
                "line_too_dense": 6.0,
            }.get(flag, 1.0)
    return max(0.0, 1.0 - penalty / (max(1, len(document.lines)) * 30.0))


def _collect_line_nodes(
    document: AlignmentDocument,
) -> tuple[dict[str, list[AlignmentWord]], dict[str, list[AlignmentSyllable]]]:
    words_by_line: dict[str, list[AlignmentWord]] = {}
    syllables_by_line: dict[str, list[AlignmentSyllable]] = {}

    for word in document.words:
        words_by_line.setdefault(word.line_id, []).append(word)
    for syllable in document.syllables:
        syllables_by_line.setdefault(syllable.line_id, []).append(syllable)

    for line_words in words_by_line.values():
        line_words.sort(key=lambda w: (w.start, w.end))
    for line_syllables in syllables_by_line.values():
        line_syllables.sort(key=lambda s: (s.start, s.end))

    return words_by_line, syllables_by_line


def _normalize_line_timing(
    line: AlignmentLine,
    words: list[AlignmentWord],
    syllables: list[AlignmentSyllable],
) -> tuple[AlignmentLine, list[AlignmentWord], list[AlignmentSyllable]]:
    """Fix mechanical anomalies in one line timing slice."""
    line = deepcopy(line)
    words = deepcopy(words)
    syllables = deepcopy(syllables)

    if not syllables:
        if line.end <= line.start:
            line.end = line.start + 0.2
        return line, words, syllables

    # 1) duration clamps
    for syl in syllables:
        if syl.end <= syl.start:
            syl.end = syl.start + _MIN_SYLLABLE
        duration = syl.end - syl.start
        if duration < _MIN_SYLLABLE:
            syl.end = syl.start + _MIN_SYLLABLE
        elif duration > _MAX_SYLLABLE:
            syl.end = syl.start + _MAX_SYLLABLE

    # 2) overlap resolution with minimal gap
    syllables.sort(key=lambda s: (s.start, s.end))
    prev_end: float | None = None
    for syl in syllables:
        if prev_end is not None and syl.start < prev_end:
            shift = (prev_end + _DEFAULT_GAP) - syl.start
            syl.start += shift
            syl.end += shift
        if syl.end <= syl.start:
            syl.end = syl.start + _MIN_SYLLABLE
        prev_end = syl.end

    # 3) density normalization (bounded)
    line_start = syllables[0].start
    line_end = syllables[-1].end
    line_duration = max(line_end - line_start, 0.001)
    density = len(syllables) / line_duration
    if density > _MAX_LINE_DENSITY:
        target_duration = len(syllables) / _MAX_LINE_DENSITY
        scale = min(2.0, target_duration / line_duration)
        for syl in syllables:
            syl.start = line_start + (syl.start - line_start) * scale
            syl.end = line_start + (syl.end - line_start) * scale
        line_end = syllables[-1].end

    # recompute words from syllables
    syllables_by_word: dict[str, list[AlignmentSyllable]] = {}
    for syl in syllables:
        syllables_by_word.setdefault(syl.word_id, []).append(syl)
    for word in words:
        linked = sorted(syllables_by_word.get(word.id, []), key=lambda s: s.start)
        if linked:
            word.start = linked[0].start
            word.end = linked[-1].end
            word.syllable_ids = [s.id for s in linked]

    line.start = min(line.start, syllables[0].start)
    line.start = syllables[0].start
    line.end = max(line.start + _MIN_SYLLABLE, syllables[-1].end)
    return line, words, syllables


def _line_local_penalties(
    line: AlignmentLine,
    syllables: list[AlignmentSyllable],
) -> dict[str, float]:
    penalties = {
        "timing": 0.0,
        "overlap": 0.0,
        "density": 0.0,
        "continuity": 0.0,
        "text_audio": 0.0,
        "structural": 0.0,
    }
    if line.end <= line.start:
        penalties["timing"] += 1.0

    # syllable checks
    for idx, syl in enumerate(syllables):
        duration = syl.end - syl.start
        if duration < _MIN_SYLLABLE:
            penalties["timing"] += 0.6
        elif duration > _MAX_SYLLABLE:
            penalties["timing"] += 0.4
        if idx > 0 and syl.start < syllables[idx - 1].end:
            penalties["overlap"] += 1.0

    line_duration = max(line.end - line.start, 0.001)
    density = len(syllables) / line_duration if syllables else 0.0
    if density > _MAX_LINE_DENSITY:
        penalties["density"] += min(
            1.0,
            (density - _MAX_LINE_DENSITY) / _MAX_LINE_DENSITY,
        )

    return penalties


def _weighted_score(penalties: dict[str, float]) -> float:
    # Weights agreed for v1.
    score = 1.0
    score -= penalties["timing"] * 0.28
    score -= penalties["overlap"] * 0.22
    score -= penalties["density"] * 0.12
    score -= penalties["continuity"] * 0.20
    score -= penalties["text_audio"] * 0.14
    score -= penalties["structural"] * 0.04
    return max(0.0, score)


def _candidate_from_line(
    line: AlignmentLine,
    words: list[AlignmentWord],
    syllables: list[AlignmentSyllable],
    kind: str,
) -> Candidate:
    norm_line, norm_words, norm_syllables = _normalize_line_timing(
        line,
        words,
        syllables,
    )
    penalties = _line_local_penalties(norm_line, norm_syllables)
    # structural-ish penalties for aggressive kinds
    if kind in {"drop", "duplicate", "reorder"}:
        penalties["structural"] += 0.2
    if kind in {"merge", "split"}:
        penalties["structural"] += 0.1
    return Candidate(
        line_id=line.id,
        kind=kind,
        line=norm_line,
        words=norm_words,
        syllables=norm_syllables,
        local_score=_weighted_score(penalties),
        penalties=penalties,
    )


def _clone_line_shape(src: AlignmentLine, new_line: AlignmentLine) -> AlignmentLine:
    cloned = deepcopy(new_line)
    cloned.text = src.text
    return cloned


def _generate_line_candidates(
    document: AlignmentDocument,
    line: AlignmentLine,
    idx: int,
    words_by_line: dict[str, list[AlignmentWord]],
    syllables_by_line: dict[str, list[AlignmentSyllable]],
) -> list[Candidate]:
    """Generate bounded line-level hypotheses.

    This is text-window inspired but uses already materialized line nodes.
    """
    base_words = words_by_line.get(line.id, [])
    base_syllables = syllables_by_line.get(line.id, [])

    candidates: list[Candidate] = []
    candidates.append(
        _candidate_from_line(line, base_words, base_syllables, kind="keep")
    )

    # Shift window ±1 using neighboring timing shapes.
    if idx > 0:
        prev_line = document.lines[idx - 1]
        candidates.append(
            _candidate_from_line(
                _clone_line_shape(line, prev_line),
                deepcopy(base_words),
                deepcopy(base_syllables),
                kind="shift_prev",
            )
        )
    if idx + 1 < len(document.lines):
        next_line = document.lines[idx + 1]
        candidates.append(
            _candidate_from_line(
                _clone_line_shape(line, next_line),
                deepcopy(base_words),
                deepcopy(base_syllables),
                kind="shift_next",
            )
        )

    # Merge-ish: stretch to include neighbor span.
    if idx > 0:
        prev_line = document.lines[idx - 1]
        merged = deepcopy(line)
        merged.start = min(prev_line.start, line.start)
        candidates.append(
            _candidate_from_line(
                merged,
                deepcopy(base_words),
                deepcopy(base_syllables),
                kind="merge",
            )
        )
    if idx + 1 < len(document.lines):
        nxt = document.lines[idx + 1]
        merged = deepcopy(line)
        merged.end = max(nxt.end, line.end)
        candidates.append(
            _candidate_from_line(
                merged,
                deepcopy(base_words),
                deepcopy(base_syllables),
                kind="merge",
            )
        )

    # Split-ish: compress to half span (conservative)
    split = deepcopy(line)
    midpoint = split.start + max(0.1, (split.end - split.start) / 2)
    split.end = midpoint
    candidates.append(
        _candidate_from_line(
            split,
            deepcopy(base_words),
            deepcopy(base_syllables),
            kind="split",
        )
    )

    # Drop weak line (keep placeholder min interval to preserve monotonic chain).
    dropped = deepcopy(line)
    dropped.end = dropped.start + 0.08
    candidates.append(_candidate_from_line(dropped, [], [], kind="drop"))

    # Duplicate probable chorus line by expanding span conservatively.
    duplicate = deepcopy(line)
    duplicate.end = duplicate.end + min(2.0, max(0.2, line.end - line.start))
    candidates.append(
        _candidate_from_line(
            duplicate,
            deepcopy(base_words),
            deepcopy(base_syllables),
            kind="duplicate",
        )
    )

    # Progressive window padding as tiny continuity penalty reduction proxies.
    for pad in _WINDOW_PADDINGS:
        widened = deepcopy(line)
        widened.start = max(0.0, widened.start - pad)
        widened.end = widened.end + pad
        cand = _candidate_from_line(
            widened,
            deepcopy(base_words),
            deepcopy(base_syllables),
            kind=f"window_{pad:.1f}",
        )
        cand.penalties["continuity"] = max(0.0, cand.penalties["continuity"] - 0.02)
        cand.local_score = _weighted_score(cand.penalties)
        candidates.append(cand)

    # Deduplicate by (kind, rounded start/end)
    unique: dict[tuple[str, int, int], Candidate] = {}
    for candidate in candidates:
        key = (
            candidate.kind,
            int(candidate.line.start * 100),
            int(candidate.line.end * 100),
        )
        if key not in unique or candidate.local_score > unique[key].local_score:
            unique[key] = candidate

    top = sorted(unique.values(), key=lambda c: c.local_score, reverse=True)
    return top[:_MAX_CANDIDATES_PER_LINE]


def _transition_penalty(prev: Candidate, nxt: Candidate) -> float:
    penalty = 0.0
    if nxt.line.start < prev.line.start:
        return 999.0
    if nxt.line.start < prev.line.end:
        penalty += 1.0
    gap = nxt.line.start - prev.line.end
    if gap > 4.0:
        penalty += min(1.0, (gap - 4.0) / 8.0)
    # Tempo distortion proxy
    prev_duration = max(0.01, prev.line.end - prev.line.start)
    nxt_duration = max(0.01, nxt.line.end - nxt.line.start)
    ratio = max(prev_duration, nxt_duration) / min(prev_duration, nxt_duration)
    if ratio > 4.0:
        penalty += min(1.0, (ratio - 4.0) / 4.0)
    return penalty


def _beam_search(
    candidate_sets: list[list[Candidate]],
) -> tuple[list[Candidate], float]:
    if not candidate_sets:
        return [], 0.0

    beams: list[tuple[list[Candidate], float]] = [([], 0.0)]

    for candidates in candidate_sets:
        new_beams: list[tuple[list[Candidate], float]] = []
        for path, path_score in beams:
            prev = path[-1] if path else None
            for candidate in candidates:
                transition = (
                    _transition_penalty(prev, candidate)
                    if prev is not None
                    else 0.0
                )
                if transition >= 999.0:
                    continue
                score = path_score + candidate.local_score - (0.20 * transition)
                new_beams.append((path + [candidate], score))

        if not new_beams:
            break
        new_beams.sort(key=lambda item: item[1], reverse=True)
        beams = new_beams[:_BEAM_WIDTH]

    best_path, best_score = max(beams, key=lambda item: item[1])
    return best_path, best_score


def _manual_lane_required(
    lyrics_source: str | None,
    flagged_ratio: float,
) -> tuple[bool, str | None]:
    if (lyrics_source or "") == "asr_fallback":
        return True, "lyrics_source_asr_fallback"
    if flagged_ratio > 0.50:
        return True, "too_many_flagged_lines"
    return False, None


def _build_document_from_path(
    source: AlignmentDocument,
    path: list[Candidate],
) -> AlignmentDocument:
    words_map = {word.id: word for word in source.words}

    by_line_candidate = {candidate.line_id: candidate for candidate in path}

    new_lines: list[AlignmentLine] = []
    new_words: list[AlignmentWord] = []
    new_syllables: list[AlignmentSyllable] = []

    for line in source.lines:
        candidate = by_line_candidate.get(line.id)
        if candidate is None:
            new_lines.append(deepcopy(line))
            continue
        new_lines.append(deepcopy(candidate.line))
        if candidate.words:
            new_words.extend(deepcopy(candidate.words))
        else:
            for word in source.words:
                if word.line_id == line.id and word.id in words_map:
                    # dropped line: omit words/syllables
                    pass
        if candidate.syllables:
            new_syllables.extend(deepcopy(candidate.syllables))

    # ensure all word<->syllable references are coherent
    kept_word_ids = {w.id for w in new_words}
    new_syllables = [s for s in new_syllables if s.word_id in kept_word_ids]
    syllable_ids_by_word: dict[str, list[str]] = {}
    for syl in new_syllables:
        syllable_ids_by_word.setdefault(syl.word_id, []).append(syl.id)

    fixed_words: list[AlignmentWord] = []
    for word in new_words:
        linked = [s for s in new_syllables if s.word_id == word.id]
        linked.sort(key=lambda s: s.start)
        if linked:
            word.start = linked[0].start
            word.end = linked[-1].end
            word.syllable_ids = [s.id for s in linked]
            fixed_words.append(word)

    # In case candidate path removed all words for some lines, keep original words
    # for untouched lines.
    touched = {candidate.line_id for candidate in path}
    untouched_line_ids = {line.id for line in source.lines if line.id not in touched}
    for word in source.words:
        if word.line_id in untouched_line_ids:
            fixed_words.append(deepcopy(word))
    for syl in source.syllables:
        if syl.line_id in untouched_line_ids:
            new_syllables.append(deepcopy(syl))

    result = deepcopy(source)
    result.lines = new_lines
    result.words = fixed_words
    result.syllables = sorted(new_syllables, key=lambda s: (s.start, s.end))
    return result


def auto_local_realign(
    document: AlignmentDocument,
    *,
    lyrics_source: str | None = None,
) -> RealignResult:
    track_start = monotonic()

    baseline = deepcopy(document)
    baseline_score = _baseline_score(baseline)

    flagged_lines = [
        line.id
        for line in baseline.lines
        if _line_flags(baseline, line.id)
    ]
    flagged_ratio = (
        (len(flagged_lines) / len(baseline.lines))
        if baseline.lines
        else 0.0
    )

    manual_lane, reason = _manual_lane_required(lyrics_source, flagged_ratio)
    if manual_lane:
        diagnostics = {
            "kind": "auto_global_realign",
            "eligible": False,
            "reason": reason,
            "baseline_score": baseline_score,
            "new_score": baseline_score,
            "accepted": False,
            "confidence": "low",
            "flagged_ratio": flagged_ratio,
            "changed_items": 0,
            "elapsed_sec": round(monotonic() - track_start, 3),
        }
        return RealignResult(document=baseline, diagnostics=diagnostics)

    words_by_line, syllables_by_line = _collect_line_nodes(baseline)

    candidate_sets: list[list[Candidate]] = []
    candidate_meta: list[dict] = []

    block_start = monotonic()
    for idx, line in enumerate(baseline.lines):
        if monotonic() - block_start > _MAX_TIME_PER_BLOCK_SEC:
            break
        generated = _generate_line_candidates(
            baseline,
            line,
            idx,
            words_by_line,
            syllables_by_line,
        )
        candidate_sets.append(generated)
        candidate_meta.append(
            {
                "line_id": line.id,
                "count": len(generated),
                "top_kind": generated[0].kind if generated else None,
                "top_score": generated[0].local_score if generated else 0.0,
            }
        )

    if monotonic() - track_start > _MAX_TIME_PER_TRACK_SEC:
        diagnostics = {
            "kind": "auto_global_realign",
            "eligible": True,
            "timed_out": True,
            "baseline_score": baseline_score,
            "new_score": baseline_score,
            "accepted": False,
            "confidence": "low",
            "flagged_ratio": flagged_ratio,
            "changed_items": 0,
            "candidate_meta": candidate_meta,
            "elapsed_sec": round(monotonic() - track_start, 3),
        }
        return RealignResult(document=baseline, diagnostics=diagnostics)

    best_path, _ = _beam_search(candidate_sets)
    candidate_document = _build_document_from_path(baseline, best_path)

    # Final hard-mechanical normalization pass line-by-line.
    c_words_by_line, c_syllables_by_line = _collect_line_nodes(candidate_document)
    fixed_lines: list[AlignmentLine] = []
    fixed_words: list[AlignmentWord] = []
    fixed_syllables: list[AlignmentSyllable] = []
    for line in candidate_document.lines:
        norm_line, norm_words, norm_syllables = _normalize_line_timing(
            line,
            c_words_by_line.get(line.id, []),
            c_syllables_by_line.get(line.id, []),
        )
        fixed_lines.append(norm_line)
        fixed_words.extend(norm_words)
        fixed_syllables.extend(norm_syllables)

    candidate_document.lines = fixed_lines
    candidate_document.words = fixed_words
    candidate_document.syllables = sorted(
        fixed_syllables,
        key=lambda s: (s.start, s.end),
    )

    new_score = _baseline_score(candidate_document)
    delta = new_score - baseline_score
    accepted = delta >= _ACCEPT_DELTA_SCORE

    if new_score >= _HIGH_CONFIDENCE:
        confidence = "high"
    elif new_score >= _MEDIUM_CONFIDENCE:
        confidence = "medium"
    else:
        confidence = "low"

    output = candidate_document if accepted else baseline

    output_flagged_lines = []
    for line in output.lines:
        flags = _line_flags(output, line.id)
        if flags:
            output_flagged_lines.append({"line_id": line.id, "flags": flags})

    changed_line_ids = []
    if accepted:
        before = {line.id: line for line in baseline.lines}
        for line in output.lines:
            prev = before.get(line.id)
            if prev is None:
                continue
            if abs(prev.start - line.start) > 0.005 or abs(prev.end - line.end) > 0.005:
                changed_line_ids.append(line.id)

    diagnostics = {
        "kind": "auto_global_realign",
        "eligible": True,
        "baseline_score": baseline_score,
        "new_score": new_score,
        "delta_score": delta,
        "accepted": accepted,
        "confidence": confidence,
        "flagged_ratio": flagged_ratio,
        "changed_items": len(changed_line_ids),
        "changed_line_ids": changed_line_ids,
        "flagged_lines": output_flagged_lines,
        "candidate_meta": candidate_meta,
        "elapsed_sec": round(monotonic() - track_start, 3),
        "params": {
            "max_candidates_per_line": _MAX_CANDIDATES_PER_LINE,
            "beam_width": _BEAM_WIDTH,
            "window_paddings": list(_WINDOW_PADDINGS),
            "accept_delta_score": _ACCEPT_DELTA_SCORE,
        },
    }
    return RealignResult(document=output, diagnostics=diagnostics)
