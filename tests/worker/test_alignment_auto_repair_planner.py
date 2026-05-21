from __future__ import annotations

from types import SimpleNamespace

from karaoke_shared.models.alignment import AlignmentLine
from karaoke_shared.models.auto_repair import AlignmentDocumentPatch, AutoRepairCluster, AutoRepairRange
from karaoke_shared.models.track import SyllableTiming

from worker.common.alignment_auto_repair import (
    AlignmentAutoRepairEngine,
    _Candidate,
    _ClusterCandidateSet,
    _ScoredCandidate,
)


def _engine() -> AlignmentAutoRepairEngine:
    return AlignmentAutoRepairEngine(
        job_service=object(),
        repo=object(),
        storage=object(),
        vad_processor=object(),
        ctc_aligner=object(),
    )


def _line(line_id: str, text: str, start: float, end: float) -> AlignmentLine:
    return AlignmentLine(id=line_id, text=text, start=start, end=end)


def _cluster(index: int, line_id: str, start: float, end: float) -> AutoRepairCluster:
    return AutoRepairCluster(
        id=f"cluster_{index}_{index}",
        line_ids=[line_id],
        start_line_index=index,
        end_line_index=index,
        old_audio_range=AutoRepairRange(start=start, end=end),
        flags=["line_too_dense"],
        root_cause_hints=["test"],
    )


def _scored(start: float, end: float, score: float, evidence: str = "asr") -> _ScoredCandidate:
    return _ScoredCandidate(
        candidate=_Candidate(
            start=start,
            end=end,
            cheap_score=score,
            reasons=("asr_phonetic: Take me out",),
            evidence_level=evidence,
            locator_method="asr_phonetic",
            matched_text="Take me out",
            locator_confidence=score,
            phoneme_score=score,
            text_score=score,
        ),
        score=score,
        confidence=score,
        timings=[SyllableTiming(syllable="Check", start=0.0, end=0.4)],
        patch=AlignmentDocumentPatch(),
        mapping=[],
        warnings=[],
        reasons=[],
    )


def test_sequence_planner_assigns_repeated_text_to_different_occurrences() -> None:
    engine = _engine()
    first = _ClusterCandidateSet(
        cluster=_cluster(1, "line_1", 8.0, 8.4),
        selected_lines=[_line("line_1", "Check me out", 8.0, 8.4)],
        text="Check me out",
        candidates=[_scored(18.62, 20.44, 0.82)],
    )
    second = _ClusterCandidateSet(
        cluster=_cluster(2, "line_2", 8.4, 8.8),
        selected_lines=[_line("line_2", "Check me out", 8.4, 8.8)],
        text="Check me out",
        candidates=[_scored(18.62, 20.44, 0.84), _scored(22.16, 22.97, 0.78)],
    )

    proposals = engine._plan_sequence([first, second], {})

    assert proposals[0].new_audio_range.start == 18.62
    assert proposals[1].new_audio_range.start == 22.16
    assert proposals[1].decision == "needs_review"


def test_vad_full_fallback_is_capped_below_review_threshold() -> None:
    engine = _engine()
    score, confidence, _reasons, warnings = engine._score_candidate(
        selected_lines=[_line("line_1", "Bring it on!", 8.82, 9.12)],
        timings=[SyllableTiming(syllable="Bring", start=8.8, end=9.1)],
        stats=SimpleNamespace(proportional_fallback=3, total_words=3),
        candidate=_Candidate(
            start=8.75,
            end=9.5,
            cheap_score=1.0,
            reasons=("VAD anchored line window",),
            evidence_level="vad",
        ),
        patch_warnings=[],
    )

    assert score <= 0.35
    assert confidence <= 0.20
    assert "full_ctc_fallback" in warnings
    assert "weak_evidence:vad" in warnings
