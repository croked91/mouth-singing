from __future__ import annotations

from types import SimpleNamespace

from karaoke_shared.models.alignment import AlignmentDocument, AlignmentLine
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
        ctc_fallback_ratio=0.0,
        query_coverage=1.0,
        match_coverage=1.0,
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
    score, confidence, _reasons, warnings, fallback_ratio, _query_coverage, _match_coverage, verification = engine._score_candidate(
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
    assert fallback_ratio == 1.0
    assert verification == "weak"
    assert "full_ctc_fallback" in warnings
    assert "weak_evidence:vad" in warnings


def test_full_fallback_low_coverage_without_neighbor_support_is_rejected() -> None:
    engine = _engine()
    candidate_set = _ClusterCandidateSet(
        cluster=_cluster(12, "line_12", 13.82, 14.68),
        selected_lines=[_line("line_12", "Negotiations breaking down", 13.82, 14.68)],
        text="Negotiations breaking down",
        candidates=[
            _ScoredCandidate(
                candidate=_Candidate(
                    start=39.76,
                    end=41.48,
                    cheap_score=1.0,
                    reasons=("asr_phonetic: to breaking down.",),
                    evidence_level="asr",
                    locator_method="asr_phonetic",
                    matched_text="to breaking down.",
                    locator_confidence=0.68,
                    phoneme_score=0.68,
                    text_score=0.76,
                ),
                score=0.78,
                confidence=0.35,
                timings=[SyllableTiming(syllable="Negotiations", start=0.0, end=0.5)],
                patch=AlignmentDocumentPatch(),
                mapping=[],
                warnings=["full_ctc_fallback"],
                reasons=[],
                ctc_fallback_ratio=1.0,
                query_coverage=2 / 3,
                match_coverage=1.0,
            )
        ],
    )

    proposal = engine._proposal_from_best_candidate(candidate_set, {})

    assert proposal.decision == "rejected"


def test_global_suspect_scope_adds_clean_lines() -> None:
    engine = _engine()
    lines = [
        _line("line_1", "first", 0.0, 0.4),
        _line("line_2", "second", 0.3, 0.6),
        _line("line_3", "third", 1.0, 2.0),
    ]
    document = AlignmentDocument(lines=lines)

    clusters, global_suspect, problem_ratio = engine._build_line_repair_units(
        document,
        10.0,
        {
            "alignment_scope": "auto",
            "global_suspect_min_lines": 1,
            "global_suspect_problem_ratio": 0.20,
        },
    )

    assert global_suspect is True
    assert problem_ratio > 0
    assert [cluster.line_ids[0] for cluster in clusters] == ["line_1", "line_2", "line_3"]
    assert "global_suspect_line" in clusters[2].flags
