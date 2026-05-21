"""Automated repair of problematic alignment fragments.

The worker owns all heavy alignment work.  Backend only enqueues jobs and
stores/apply reports.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import structlog

from karaoke_shared.alignment import document_to_syllable_timings, lyrics_text_from_document
from karaoke_shared.models.alignment import (
    AlignmentDocument,
    AlignmentLine,
    AlignmentRevision,
    AlignmentSyllable,
    AlignmentWord,
)
from karaoke_shared.models.auto_repair import (
    AlignmentDocumentPatch,
    AutoRepairCluster,
    AutoRepairLineMapping,
    AutoRepairProposal,
    AutoRepairRange,
    AutoRepairReport,
    AutoRepairSummary,
)
from karaoke_shared.models.job import Job
from karaoke_shared.models.track import SyllableTiming
from karaoke_shared.repositories.pg_repository import PgRepository
from karaoke_shared.services.job_service import JobService
from karaoke_shared.storage import S3Storage
from karaoke_shared.utils.syllabifier import Syllabifier

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class _Candidate:
    start: float
    end: float
    cheap_score: float
    reasons: tuple[str, ...]
    evidence_level: str = "current"
    locator_method: str | None = None
    matched_text: str | None = None
    locator_confidence: float | None = None
    phoneme_score: float | None = None
    text_score: float | None = None


@dataclass(frozen=True)
class _ScoredCandidate:
    candidate: _Candidate
    score: float
    confidence: float
    timings: list[SyllableTiming]
    patch: AlignmentDocumentPatch
    mapping: list[AutoRepairLineMapping]
    warnings: list[str]
    reasons: list[str]
    ctc_fallback_ratio: float
    query_coverage: float
    match_coverage: float
    neighbor_support_score: float = 0.0
    verification_level: str = "weak"


@dataclass(frozen=True)
class _ClusterCandidateSet:
    cluster: AutoRepairCluster
    selected_lines: list[AlignmentLine]
    text: str
    candidates: list[_ScoredCandidate]


class AlignmentAutoRepairEngine:
    """Find and propose repairs for alignment anomalies."""

    def __init__(
        self,
        job_service: JobService,
        repo: PgRepository,
        storage: S3Storage,
        vad_processor,
        ctc_aligner,
        whisper_model_size: str = "tiny",
        whisper_device: str = "cuda",
        whisper_compute_type: str = "float16",
        model_cache_dir: str | None = None,
    ) -> None:
        self.job_service = job_service
        self.repo = repo
        self.storage = storage
        self.vad_processor = vad_processor
        self.ctc_aligner = ctc_aligner
        self.syllabifier = Syllabifier()
        self.whisper_model_size = whisper_model_size
        self.whisper_device = whisper_device
        self.whisper_compute_type = whisper_compute_type
        self.model_cache_dir = model_cache_dir

    async def process(self, job: Job) -> None:
        data = job.data or {}
        track_id = data.get("track_id") or job.track_id
        revision_id = data.get("revision_id")
        audio_key = data.get("audio_key") or job.mp3_key
        if not track_id or not revision_id or not audio_key:
            await self.job_service.mark_permanently_failed(
                job.id,
                "Auto-repair job is missing track_id, revision_id, or audio_key.",
            )
            return

        try:
            await self.job_service.mark_step(job.id, "auto_repair_loading", 5)
            track = await self.repo.get_track(track_id)
            revision = await self.repo.get_alignment_revision(revision_id)
            if track is None:
                raise RuntimeError(f"Track '{track_id}' not found")
            if revision is None or revision.document is None:
                raise RuntimeError(f"Alignment revision '{revision_id}' has no document")

            duration = float(track.duration_sec or self._document_end(revision.document))
            language = track.language or "en"

            with tempfile.TemporaryDirectory(prefix=f"auto_repair_{job.id}_") as tmp:
                audio_path = str(Path(tmp) / "review_vocal.mp3")
                await self.storage.download_to_file(audio_key, audio_path)

                await self.job_service.mark_step(job.id, "auto_repair_analyzing", 15)
                vad_segments = await self._safe_vad(audio_path)
                phrase_locator, asr_words = await self._load_phrase_locator(
                    track_id=track_id,
                    audio_key=audio_key,
                    audio_path=audio_path,
                    language=language,
                )
                clusters, global_suspect, problem_ratio = self._build_line_repair_units(
                    revision.document,
                    duration,
                    data,
                )
                clusters = self._add_phrase_locator_repair_units(
                    document=revision.document,
                    duration=duration,
                    clusters=clusters,
                    phrase_locator=phrase_locator,
                    asr_words=asr_words,
                    language=language,
                    config=data,
                )

                candidate_sets: list[_ClusterCandidateSet] = []
                for index, cluster in enumerate(clusters):
                    progress = 20 + int((index / max(1, len(clusters))) * 65)
                    await self.job_service.mark_step(
                        job.id,
                        f"auto_repair_line_{index + 1}_of_{len(clusters)}",
                        progress,
                    )
                    candidate_set = await self._score_cluster_candidates(
                            job_id=job.id,
                            document=revision.document,
                            cluster=cluster,
                            audio_path=audio_path,
                            vad_segments=vad_segments,
                            phrase_locator=phrase_locator,
                            asr_words=asr_words,
                            duration=duration,
                            language=language,
                            config=data,
                        )
                    candidate_sets.append(candidate_set)

                if data.get("enable_sequence_planner", True):
                    proposals = self._plan_sequence(candidate_sets, data)
                else:
                    proposals = [
                        self._proposal_from_best_candidate(candidate_set, data)
                        for candidate_set in candidate_sets
                    ]

            report = AutoRepairReport(
                job_id=job.id,
                track_id=track_id,
                base_revision_id=revision.id,
                source_audio_key=audio_key,
                alignment_scope=data.get("alignment_scope", "auto"),
                global_suspect=global_suspect,
                global_suspect_problem_ratio=problem_ratio,
                status="ok" if proposals or not clusters else "partial",
                summary=self._summary(clusters, proposals),
                clusters=clusters,
                proposals=proposals,
                warnings=self._report_warnings(clusters, global_suspect),
            )

            created_revision_id = None
            if data.get("mode") == "auto_apply_safe":
                safe = [p for p in proposals if p.decision == "auto_apply"]
                if safe:
                    created_revision_id = await self._create_auto_revision(
                        revision,
                        safe,
                        job.id,
                    )
                    report.created_revision_id = created_revision_id

            await self.job_service.mark_step(job.id, "auto_repair_done", 100)
            await self.job_service.mark_completed(job.id, report.model_dump())
        except Exception as exc:  # noqa: BLE001
            logger.exception("alignment_auto_repair_failed", job_id=job.id)
            await self.job_service.mark_permanently_failed(job.id, str(exc))

    async def _load_phrase_locator(
        self,
        track_id: str,
        audio_key: str,
        audio_path: str,
        language: str,
    ):
        from worker.common.phrase_locator import PhraseLocator

        locator = PhraseLocator(
            storage=self.storage,
            track_id=track_id,
            audio_key=audio_key,
            model_size=self.whisper_model_size,
            device=self.whisper_device,
            compute_type=self.whisper_compute_type,
            model_cache_dir=self.model_cache_dir,
        )
        try:
            words = await locator.get_word_stream(audio_path, language)
            locator.cleanup()
            return locator, words
        except Exception as exc:  # noqa: BLE001
            logger.warning("phrase_locator_word_stream_failed", error=str(exc))
            locator.cleanup()
            return locator, []

    async def _safe_vad(self, audio_path: str) -> list[tuple[float, float]]:
        try:
            result = await asyncio.to_thread(self.vad_processor.process, audio_path)
            return list(result.segments or [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("auto_repair_vad_failed", error=str(exc))
            return []

    def _build_line_repair_units(
        self,
        document: AlignmentDocument,
        duration: float,
        config: dict,
    ) -> tuple[list[AutoRepairCluster], bool, float]:
        """Return one repair unit per problematic line.

        Earlier versions merged adjacent problematic lines into one CTC job.
        That is fast, but it can produce a good timing for the first line while
        smearing the next line into the same candidate.  Auto-repair should be
        conservative and independently applicable, so each problematic line is
        searched/scored on its own.
        """
        line_flags = [
            self._line_flags(document, line, index)
            for index, line in enumerate(document.lines)
        ]
        non_empty_indices = [
            index for index, line in enumerate(document.lines)
            if line.text.strip()
        ]
        bad_indices = [
            index for index in non_empty_indices
            if line_flags[index]
        ]
        problem_ratio = len(bad_indices) / max(1, len(non_empty_indices))
        max_consecutive = self._max_consecutive_indices(bad_indices)
        scope = str(config.get("alignment_scope", "auto"))
        global_suspect = (
            scope == "all_lines"
            or (
                scope == "auto"
                and len(non_empty_indices) >= int(config.get("global_suspect_min_lines", 12))
                and (
                    problem_ratio >= float(config.get("global_suspect_problem_ratio", 0.20))
                    or max_consecutive >= int(config.get("global_suspect_consecutive_problem_lines", 5))
                )
            )
        )
        if scope == "all_lines" or global_suspect:
            respect_reviewed = bool(config.get("respect_reviewed_lines", True))
            target_indices = [
                index for index in non_empty_indices
                if not (respect_reviewed and self._line_is_reviewed(document.lines[index]))
            ]
            for index in target_indices:
                if not line_flags[index]:
                    line_flags[index] = ["global_suspect_line"]
            indices = target_indices
        else:
            indices = bad_indices

        clusters = [
            self._make_cluster(document, line_flags, index, index, duration)
            for index in indices
        ]
        return clusters, global_suspect, round(problem_ratio, 4)

    @staticmethod
    def _max_consecutive_indices(indices: list[int]) -> int:
        if not indices:
            return 0
        longest = 1
        current = 1
        previous = indices[0]
        for index in indices[1:]:
            if index == previous + 1:
                current += 1
                longest = max(longest, current)
            else:
                current = 1
            previous = index
        return longest

    @staticmethod
    def _line_is_reviewed(line: AlignmentLine) -> bool:
        reviewed_flags = {"reviewed", "verified", "manually_reviewed", "manual_verified"}
        return bool(reviewed_flags.intersection(set(line.flags)))

    @staticmethod
    def _report_warnings(clusters: list[AutoRepairCluster], global_suspect: bool) -> list[str]:
        warnings: list[str] = []
        if not clusters:
            warnings.append(
                "Разметка трека глобально подозрительна, но подходящие строки не найдены."
                if global_suspect
                else "Проблемные участки не найдены."
            )
        return warnings

    def _make_cluster(
        self,
        document: AlignmentDocument,
        line_flags: list[list[str]],
        start: int,
        end: int,
        duration: float,
    ) -> AutoRepairCluster:
        line_ids = [line.id for line in document.lines[start : end + 1]]
        flags = sorted({flag for idx in range(start, end + 1) for flag in line_flags[idx]})
        old_start = max(0.0, min(line.start for line in document.lines[start : end + 1]))
        old_end = min(duration, max(line.end for line in document.lines[start : end + 1]))
        hints = self._root_cause_hints(flags, document.lines[start : end + 1])
        return AutoRepairCluster(
            id=f"cluster_{start + 1}_{end + 1}",
            line_ids=line_ids,
            start_line_index=start,
            end_line_index=end,
            old_audio_range=AutoRepairRange(start=old_start, end=old_end),
            flags=flags,
            root_cause_hints=hints,
        )

    def _add_phrase_locator_repair_units(
        self,
        document: AlignmentDocument,
        duration: float,
        clusters: list[AutoRepairCluster],
        phrase_locator,
        asr_words: list,
        language: str,
        config: dict,
    ) -> list[AutoRepairCluster]:
        if not phrase_locator or not asr_words:
            return clusters
        existing_indices = {cluster.start_line_index for cluster in clusters}
        line_flags = [
            self._line_flags(document, line, index)
            for index, line in enumerate(document.lines)
        ]
        result = list(clusters)
        threshold = float(config.get("phrase_locator_suspicion_threshold", 0.62))
        for index, line in enumerate(document.lines):
            if index in existing_indices or not line.text.strip():
                continue
            candidates = phrase_locator.locate(
                query_text=line.text,
                words=asr_words,
                language=language,
                old_start=line.start,
                old_end=line.end,
                track_duration=duration,
                limit=3,
                threshold=threshold,
            )
            if not candidates:
                continue
            best = candidates[0]
            current_center = (line.start + line.end) / 2.0
            located_center = (best.start + best.end) / 2.0
            old_duration = max(0.3, line.end - line.start)
            if abs(located_center - current_center) < max(3.0, old_duration * 2.0):
                continue
            line_flags[index] = sorted(set(line_flags[index]) | {"asr_evidence_far_from_current_timing"})
            result.append(self._make_cluster(document, line_flags, index, index, duration))
        return sorted(result, key=lambda item: item.start_line_index)

    async def _score_cluster_candidates(
        self,
        job_id: str,
        document: AlignmentDocument,
        cluster: AutoRepairCluster,
        audio_path: str,
        vad_segments: list[tuple[float, float]],
        phrase_locator,
        asr_words: list,
        duration: float,
        language: str,
        config: dict,
    ) -> _ClusterCandidateSet:
        selected_lines = [
            line for line in document.lines if line.id in set(cluster.line_ids)
        ]
        if not selected_lines:
            return _ClusterCandidateSet(cluster=cluster, selected_lines=[], text="", candidates=[])
        text = "\n".join(line.text for line in selected_lines if line.text.strip())
        if not text.strip():
            return _ClusterCandidateSet(cluster=cluster, selected_lines=selected_lines, text="", candidates=[])

        candidates = self._generate_candidates(
            cluster,
            selected_lines,
            vad_segments,
            phrase_locator,
            asr_words,
            duration,
            language,
            config,
        )
        scored: list[_ScoredCandidate] = []
        ctc_limit = int(config.get("max_ctc_candidates_per_line", config.get("max_ctc_candidates", 72)))
        for candidate in candidates[:ctc_limit]:
            try:
                scored_candidate = await self._run_candidate(
                    job_id,
                    document,
                    selected_lines,
                    text,
                    candidate,
                    audio_path,
                    language,
                )
                scored.append(scored_candidate)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "auto_repair_candidate_failed",
                    job_id=job_id,
                    cluster_id=cluster.id,
                    start=candidate.start,
                    end=candidate.end,
                    error=str(exc),
                )

        scored.sort(key=lambda item: item.score, reverse=True)
        return _ClusterCandidateSet(
            cluster=cluster,
            selected_lines=selected_lines,
            text=text,
            candidates=scored,
        )

    def _proposal_from_best_candidate(
        self,
        candidate_set: _ClusterCandidateSet,
        config: dict,
        planner_score: float | None = None,
        sequence_group_id: str | None = None,
    ) -> AutoRepairProposal:
        cluster = candidate_set.cluster
        selected_lines = candidate_set.selected_lines
        text = candidate_set.text
        scored = candidate_set.candidates
        if not selected_lines or not text.strip():
            return self._blocked_proposal(cluster, selected_lines, text, "Строка не содержит текста для автоисправления.")
        if not scored:
            return self._blocked_proposal(cluster, selected_lines, text, "Все варианты выравнивания завершились ошибкой.")

        best = scored[0]
        second_score = scored[1].score if len(scored) > 1 else 0.0
        margin = max(0.0, best.score - second_score)
        auto_threshold = float(config.get("auto_apply_threshold", 0.90))
        review_threshold = float(config.get("review_threshold", 0.72))
        critical_warnings = any("critical" in warning for warning in best.warnings)
        decision_score = min(best.score, planner_score) if planner_score is not None else best.score
        if decision_score >= auto_threshold and margin >= 0.10 and not critical_warnings:
            decision = "auto_apply"
        elif decision_score >= review_threshold:
            decision = "needs_review"
        else:
            decision = "rejected"
        if (
            best.ctc_fallback_ratio >= 0.999
            and best.query_coverage < 0.75
            and best.neighbor_support_score < 0.50
        ):
            decision = "rejected"

        proposal = AutoRepairProposal(
            id=f"proposal_{cluster.id}_{self._range_key(best.candidate.start, best.candidate.end)}",
            cluster_id=cluster.id,
            decision=decision,
            root_cause_hints=cluster.root_cause_hints,
            score=round(decision_score, 4),
            confidence=round(best.confidence, 4),
            margin=round(margin, 4),
            line_ids=cluster.line_ids,
            text=text,
            old_audio_range=cluster.old_audio_range,
            new_audio_range=AutoRepairRange(start=best.candidate.start, end=best.candidate.end),
            syllable_timings=best.timings,
            line_mapping=best.mapping,
            document_patch=best.patch,
            reasons=best.reasons,
            warnings=best.warnings,
            locator_method=best.candidate.locator_method,
            matched_text=best.candidate.matched_text,
            locator_confidence=best.candidate.locator_confidence,
            phoneme_score=best.candidate.phoneme_score,
            text_score=best.candidate.text_score,
            planner_score=planner_score,
            evidence_level=best.candidate.evidence_level,  # type: ignore[arg-type]
            sequence_group_id=sequence_group_id,
            ctc_fallback_ratio=best.ctc_fallback_ratio,
            query_coverage=best.query_coverage,
            match_coverage=best.match_coverage,
            neighbor_support_score=best.neighbor_support_score,
            verification_level=best.verification_level,  # type: ignore[arg-type]
        )
        return proposal

    def _plan_sequence(
        self,
        candidate_sets: list[_ClusterCandidateSet],
        config: dict,
    ) -> list[AutoRepairProposal]:
        if not candidate_sets:
            return []

        ordered = sorted(candidate_sets, key=lambda item: item.cluster.start_line_index)
        beam_width = int(config.get("sequence_planner_beam_width", 24))
        per_line_limit = int(config.get("sequence_planner_candidates_per_line", 10))
        beam: list[tuple[float, float, float | None, list[tuple[float, float, str]], list[tuple[_ClusterCandidateSet, _ScoredCandidate | None, float]]]] = [
            (0.0, 0.0, None, [], [])
        ]

        for candidate_set in ordered:
            next_beam: list[tuple[float, float, float | None, list[tuple[float, float, str]], list[tuple[_ClusterCandidateSet, _ScoredCandidate | None, float]]]] = []
            text_key = self._normalize_for_sequence(candidate_set.text)
            candidates = candidate_set.candidates[:per_line_limit]
            for total_score, previous_end, previous_delta, used_ranges, selected in beam:
                # Keep a blocked option so one bad line does not force the planner
                # to pick a weak VAD/current candidate.
                block_penalty = -0.70 if candidates else -0.08
                if candidates:
                    # When repeated text has equivalent candidates, prefer keeping
                    # the earlier line and blocking the later duplicate.
                    block_penalty -= max(0.0, 100.0 - candidate_set.cluster.start_line_index) * 0.002
                next_beam.append(
                    (
                        total_score + block_penalty,
                        previous_end,
                        previous_delta,
                        used_ranges,
                        selected + [(candidate_set, None, 0.0)],
                    )
                )
                for scored in candidates:
                    candidate = scored.candidate
                    overlap_blocked = False
                    overlap_penalty = 0.0
                    for used_start, used_end, used_text_key in used_ranges:
                        overlap = self._range_overlap_ratio(
                            candidate.start,
                            candidate.end,
                            used_start,
                            used_end,
                        )
                        if overlap > 0.25:
                            overlap_blocked = True
                            break
                        if text_key and text_key == used_text_key and overlap > 0.05:
                            overlap_penalty += 0.35
                    if overlap_blocked:
                        continue

                    order_penalty = 0.0
                    if previous_end and candidate.start < previous_end - 0.05:
                        order_penalty = min(0.65, (previous_end - candidate.start) / 6.0)

                    candidate_delta = candidate.start - candidate_set.cluster.old_audio_range.start
                    offset_penalty = self._sequence_offset_penalty(previous_delta, candidate_delta)
                    evidence_bonus = {
                        "split_asr": 0.10,
                        "asr": 0.07,
                        "vad": -0.10,
                        "grid": -0.16,
                        "current": -0.18,
                    }.get(candidate.evidence_level, -0.10)
                    planner_score = max(
                        0.0,
                        min(
                            1.0,
                            scored.score
                            + evidence_bonus
                            - overlap_penalty
                            - order_penalty
                            - offset_penalty,
                        ),
                    )
                    if planner_score < 0.40:
                        continue
                    next_beam.append(
                        (
                            total_score + planner_score,
                            max(previous_end, candidate.end),
                            candidate_delta,
                            used_ranges + [(candidate.start, candidate.end, text_key)],
                            selected + [(candidate_set, scored, planner_score)],
                        )
                    )
            next_beam.sort(key=lambda item: item[0], reverse=True)
            beam = next_beam[:beam_width] or beam

        best = max(beam, key=lambda item: item[0])
        selected_path = self._with_neighbor_support(best[4])
        proposals: list[AutoRepairProposal] = []
        for candidate_set, selected_candidate, planner_score in selected_path:
            if selected_candidate is None:
                proposals.append(
                    self._blocked_proposal(
                        candidate_set.cluster,
                        candidate_set.selected_lines,
                        candidate_set.text,
                        "Sequence planner не нашёл надёжный непересекающийся вариант.",
                    )
                )
                continue
            reordered_candidates = [
                selected_candidate,
                *[candidate for candidate in candidate_set.candidates if candidate is not selected_candidate],
            ]
            proposal = self._proposal_from_best_candidate(
                _ClusterCandidateSet(
                    cluster=candidate_set.cluster,
                    selected_lines=candidate_set.selected_lines,
                    text=candidate_set.text,
                    candidates=reordered_candidates,
                ),
                config,
                planner_score=round(planner_score, 4),
                sequence_group_id="auto_repair_sequence",
            )
            proposals.append(proposal)
        return proposals

    def _with_neighbor_support(
        self,
        selected_path: list[tuple[_ClusterCandidateSet, _ScoredCandidate | None, float]],
    ) -> list[tuple[_ClusterCandidateSet, _ScoredCandidate | None, float]]:
        result: list[tuple[_ClusterCandidateSet, _ScoredCandidate | None, float]] = []
        for index, (candidate_set, selected_candidate, planner_score) in enumerate(selected_path):
            if selected_candidate is None:
                result.append((candidate_set, selected_candidate, planner_score))
                continue
            support = 0.0
            current_delta = selected_candidate.candidate.start - candidate_set.cluster.old_audio_range.start
            for neighbor_index in (index - 1, index + 1):
                if neighbor_index < 0 or neighbor_index >= len(selected_path):
                    continue
                neighbor_set, neighbor_candidate, _neighbor_score = selected_path[neighbor_index]
                if neighbor_candidate is None:
                    continue
                neighbor_delta = neighbor_candidate.candidate.start - neighbor_set.cluster.old_audio_range.start
                gap = abs(neighbor_candidate.candidate.start - selected_candidate.candidate.start)
                if abs(neighbor_delta - current_delta) <= 4.0 and gap <= 18.0:
                    support += 0.5
            support = min(1.0, support)
            verification_level = selected_candidate.verification_level
            if support >= 0.5 and verification_level == "weak":
                verification_level = "medium"
            result.append(
                (
                    candidate_set,
                    replace(
                        selected_candidate,
                        neighbor_support_score=round(support, 4),
                        verification_level=verification_level,
                    ),
                    planner_score,
                )
            )
        return result

    @staticmethod
    def _range_overlap_ratio(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
        overlap = max(0.0, min(a_end, b_end) - max(a_start, b_start))
        shortest = max(0.001, min(a_end - a_start, b_end - b_start))
        return overlap / shortest

    @staticmethod
    def _normalize_for_sequence(text: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^0-9a-zа-яё]+", " ", text.lower())).strip()

    @classmethod
    def _text_coverage(cls, query_text: str, matched_text: str) -> tuple[float, float]:
        query_tokens = cls._normalize_for_sequence(query_text).split()
        match_tokens = cls._normalize_for_sequence(matched_text).split()
        if not query_tokens or not match_tokens:
            return 0.0, 0.0
        remaining = list(match_tokens)
        overlap = 0
        for token in query_tokens:
            if token in remaining:
                remaining.remove(token)
                overlap += 1
        return overlap / len(query_tokens), overlap / len(match_tokens)

    @staticmethod
    def _sequence_offset_penalty(previous_delta: float | None, candidate_delta: float) -> float:
        if previous_delta is None:
            return 0.0
        jump = abs(candidate_delta - previous_delta)
        if jump <= 4.0:
            return 0.0
        if jump <= 8.0:
            return (jump - 4.0) * 0.04
        return min(0.95, 0.20 + (jump - 8.0) * 0.075)

    async def _run_candidate(
        self,
        job_id: str,
        document: AlignmentDocument,
        selected_lines: list[AlignmentLine],
        text: str,
        candidate: _Candidate,
        audio_path: str,
        language: str,
    ) -> _ScoredCandidate:
        with tempfile.TemporaryDirectory(prefix=f"auto_candidate_{job_id}_") as tmp:
            fragment_path = str(Path(tmp) / "fragment.wav")
            await self._slice_audio(audio_path, fragment_path, candidate.start, candidate.end)
            timings, stats = await asyncio.to_thread(
                self.ctc_aligner.align,
                fragment_path,
                text,
                language,
            )

        absolute_timings = [
            SyllableTiming(
                syllable=timing.syllable,
                start=round(candidate.start + timing.start, 3),
                end=round(candidate.start + timing.end, 3),
            )
            for timing in timings
        ]
        patch, mapping, patch_warnings = self._build_patch(
            document,
            selected_lines,
            absolute_timings,
            language,
            proposal_seed=f"{job_id}:{candidate.start:.3f}:{candidate.end:.3f}",
        )
        (
            score,
            confidence,
            reasons,
            score_warnings,
            ctc_fallback_ratio,
            query_coverage,
            match_coverage,
            verification_level,
        ) = self._score_candidate(
            selected_lines,
            absolute_timings,
            stats,
            candidate,
            patch_warnings,
        )
        return _ScoredCandidate(
            candidate=candidate,
            score=score,
            confidence=confidence,
            timings=timings,
            patch=patch,
            mapping=mapping,
            warnings=patch_warnings + score_warnings,
            reasons=list(candidate.reasons) + reasons,
            ctc_fallback_ratio=ctc_fallback_ratio,
            query_coverage=query_coverage,
            match_coverage=match_coverage,
            verification_level=verification_level,
        )

    async def _slice_audio(
        self,
        source_path: str,
        target_path: str,
        start: float,
        end: float,
    ) -> None:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-to",
            f"{end:.3f}",
            "-i",
            source_path,
            "-ac",
            "1",
            "-ar",
            "16000",
            target_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode != 0:
            raise RuntimeError("Failed to slice audio fragment")

    def _generate_candidates(
        self,
        cluster: AutoRepairCluster,
        selected_lines: list[AlignmentLine],
        vad_segments: list[tuple[float, float]],
        phrase_locator,
        asr_words: list,
        duration: float,
        language: str,
        config: dict,
    ) -> list[_Candidate]:
        old_start = cluster.old_audio_range.start
        old_end = cluster.old_audio_range.end
        max_audio_seconds = float(config.get("max_audio_seconds", 60.0))
        search_radius = float(config.get("line_search_radius_sec", 12.0))
        grid_step = float(config.get("line_search_step_sec", 0.25))
        raw: list[_Candidate] = []
        text = "\n".join(line.text for line in selected_lines if line.text.strip())

        if phrase_locator and asr_words and text.strip():
            locator_candidates = phrase_locator.locate(
                query_text=text,
                words=asr_words,
                language=language,
                old_start=old_start,
                old_end=old_end,
                track_duration=duration,
                limit=int(config.get("max_locator_candidates_per_line", config.get("phrase_locator_candidates", 8))),
                threshold=float(config.get("phrase_locator_threshold", 0.55)),
            )
            for candidate in locator_candidates:
                raw.append(
                    _Candidate(
                        start=max(0.0, candidate.start - 0.2),
                        end=min(duration, candidate.end + 0.2),
                        cheap_score=1.0 + candidate.confidence,
                        reasons=(
                            f"{candidate.method}: {candidate.matched_text}",
                            f"locator confidence: {candidate.confidence:.2f}",
                        ),
                        evidence_level="asr",
                        locator_method=candidate.method,
                        matched_text=candidate.matched_text,
                        locator_confidence=candidate.confidence,
                        phoneme_score=candidate.phoneme_score,
                        text_score=candidate.text_score,
                    )
                )
            if config.get("enable_split_phrase_locator", True):
                raw.extend(
                    self._split_phrase_candidates(
                        phrase_locator,
                        asr_words,
                        text,
                        old_start,
                        old_end,
                        duration,
                        language,
                        config,
                    )
                )

        for pad in (0.5, 1.0, 2.0, 4.0):
            raw.append(self._candidate(old_start - pad, old_end + pad, duration, "current range padded"))

        shifts = (-4.0, -2.0, -1.0, -0.5, 0.5, 1.0, 2.0, 4.0)
        for start_shift in shifts:
            for end_shift in shifts:
                raw.append(
                    self._candidate(
                        old_start + start_shift,
                        old_end + end_shift,
                        duration,
                        "shifted boundaries",
                    )
                )

        expected_syllables = sum(
            len(self.syllabifier.split_text_to_syllables(line.text, language)[0])
            for line in selected_lines
        )
        old_duration = max(0.3, old_end - old_start)
        plausible_durations = {
            old_duration * factor
            for factor in (0.55, 0.7, 0.85, 1.0, 1.2, 1.45, 1.75)
        }
        plausible_durations.update(
            expected_syllables / rate
            for rate in (2.2, 3.0, 4.0, 5.2, 6.5, 7.8)
            if expected_syllables > 0
        )

        search_start = max(0.0, old_start - search_radius)
        search_end = min(duration, old_end + search_radius)
        nearby_vad = [
            (start, end)
            for start, end in vad_segments
            if end >= search_start and start <= search_end
        ]
        for start, end in nearby_vad:
            raw.append(self._candidate(start - 0.2, end + 0.2, duration, "VAD phrase boundary"))
            for candidate_duration in plausible_durations:
                if candidate_duration <= 0:
                    continue
                raw.append(
                    self._candidate(
                        start - 0.1,
                        start - 0.1 + candidate_duration,
                        duration,
                        "VAD anchored line window",
                    )
                )
                raw.append(
                    self._candidate(
                        end + 0.1 - candidate_duration,
                        end + 0.1,
                        duration,
                        "VAD anchored line window",
                    )
                )
        if nearby_vad:
            raw.append(
                self._candidate(
                    min(start for start, _ in nearby_vad) - 0.2,
                    max(end for _, end in nearby_vad) + 0.2,
                    duration,
                    "merged VAD region",
                )
            )

        for candidate_duration in sorted(plausible_durations):
            if candidate_duration < 0.3 or candidate_duration > max_audio_seconds:
                continue
            start = search_start
            last_start = max(search_start, search_end - candidate_duration)
            while start <= last_start + 0.001:
                raw.append(
                    self._candidate(
                        start,
                        start + candidate_duration,
                        duration,
                        "line grid search",
                    )
                )
                start += max(0.05, grid_step)

        dedup: dict[tuple[int, int], _Candidate] = {}
        for candidate in raw:
            if candidate.end <= candidate.start:
                continue
            candidate_duration = candidate.end - candidate.start
            if candidate_duration < 0.3 or candidate_duration > max_audio_seconds:
                continue
            rate = expected_syllables / max(0.1, candidate_duration)
            if rate < 0.8 or rate > 12.0:
                continue
            key = (round(candidate.start * 10), round(candidate.end * 10))
            cheap_score = (
                candidate.cheap_score
                + self._duration_plausibility(rate) * 0.35
                + self._vad_overlap_score(candidate, nearby_vad) * 0.25
            )
            candidate = _Candidate(
                start=candidate.start,
                end=candidate.end,
                cheap_score=cheap_score,
                reasons=candidate.reasons,
                evidence_level=candidate.evidence_level,
                locator_method=candidate.locator_method,
                matched_text=candidate.matched_text,
                locator_confidence=candidate.locator_confidence,
                phoneme_score=candidate.phoneme_score,
                text_score=candidate.text_score,
            )
            prev = dedup.get(key)
            if prev is None or candidate.cheap_score > prev.cheap_score:
                dedup[key] = candidate

        return sorted(dedup.values(), key=lambda item: item.cheap_score, reverse=True)

    def _split_phrase_candidates(
        self,
        phrase_locator,
        asr_words: list,
        text: str,
        old_start: float,
        old_end: float,
        duration: float,
        language: str,
        config: dict,
    ) -> list[_Candidate]:
        parts = self._split_query_parts(text)
        if len(parts) < 2:
            return []
        limit = max(4, int(config.get("max_locator_candidates_per_line", 20)) // 2)
        threshold = max(0.50, float(config.get("phrase_locator_threshold", 0.55)) - 0.05)
        first_candidates = phrase_locator.locate(
            query_text=parts[0],
            words=asr_words,
            language=language,
            old_start=old_start,
            old_end=old_end,
            track_duration=duration,
            limit=limit,
            threshold=threshold,
        )
        last_candidates = phrase_locator.locate(
            query_text=parts[-1],
            words=asr_words,
            language=language,
            old_start=old_start,
            old_end=old_end,
            track_duration=duration,
            limit=limit,
            threshold=threshold,
        )
        result: list[_Candidate] = []
        max_audio_seconds = float(config.get("max_audio_seconds", 60.0))
        for first in first_candidates:
            for last in last_candidates:
                if last.end <= first.start:
                    continue
                start = max(0.0, first.start - 0.25)
                end = min(duration, last.end + 0.25)
                if end - start > max_audio_seconds:
                    continue
                confidence = min(first.confidence, last.confidence)
                result.append(
                    _Candidate(
                        start=start,
                        end=end,
                        cheap_score=1.15 + confidence,
                        reasons=(
                            f"split_asr: {first.matched_text} ... {last.matched_text}",
                            f"split locator confidence: {confidence:.2f}",
                        ),
                        evidence_level="split_asr",
                        locator_method="split_asr_phonetic",
                        matched_text=f"{first.matched_text} ... {last.matched_text}",
                        locator_confidence=round(confidence, 4),
                        phoneme_score=round((first.phoneme_score + last.phoneme_score) / 2.0, 4),
                        text_score=round((first.text_score + last.text_score) / 2.0, 4),
                    )
                )
        return result

    @staticmethod
    def _split_query_parts(text: str) -> list[str]:
        import re

        normalized = " ".join(text.split())
        raw_parts = re.split(r"\.{2,}|[;:—–-]", normalized)
        parts = [part.strip() for part in raw_parts if len(part.strip().split()) >= 2]
        if len(parts) >= 2:
            return [parts[0], parts[-1]]
        return []

    @staticmethod
    def _vad_overlap_score(
        candidate: _Candidate,
        vad_segments: list[tuple[float, float]],
    ) -> float:
        if not vad_segments:
            return 0.0
        duration = max(0.001, candidate.end - candidate.start)
        overlap = 0.0
        for start, end in vad_segments:
            overlap += max(0.0, min(candidate.end, end) - max(candidate.start, start))
        return max(0.0, min(1.0, overlap / duration))

    @staticmethod
    def _candidate(start: float, end: float, duration: float, reason: str) -> _Candidate:
        start = max(0.0, min(duration, start))
        end = max(0.0, min(duration, end))
        if reason.startswith("VAD") or reason == "merged VAD region":
            evidence_level = "vad"
        elif "grid" in reason:
            evidence_level = "grid"
        else:
            evidence_level = "current"
        return _Candidate(
            start=start,
            end=end,
            cheap_score=0.5,
            reasons=(reason,),
            evidence_level=evidence_level,
        )

    def _build_patch(
        self,
        document: AlignmentDocument,
        selected_lines: list[AlignmentLine],
        timings: list[SyllableTiming],
        language: str,
        proposal_seed: str,
    ) -> tuple[AlignmentDocumentPatch, list[AutoRepairLineMapping], list[str]]:
        warnings: list[str] = []
        expected_counts = [
            max(1, len(self.syllabifier.split_text_to_syllables(line.text, language)[0]))
            for line in selected_lines
        ]
        total_expected = sum(expected_counts)
        if total_expected != len(timings):
            warnings.append("Количество слогов отличается от ожидаемого.")
            expected_counts = self._fit_counts(expected_counts, len(timings))

        words_by_line = {
            line.id: [word for word in document.words if word.line_id == line.id]
            for line in selected_lines
        }
        syllables_by_line = {
            line.id: [syl for syl in document.syllables if syl.line_id == line.id]
            for line in selected_lines
        }

        replace_lines: list[AlignmentLine] = []
        replace_words: list[AlignmentWord] = []
        replace_syllables: list[AlignmentSyllable] = []
        remove_word_ids: list[str] = []
        remove_syllable_ids: list[str] = []
        mapping: list[AutoRepairLineMapping] = []

        cursor = 0
        for line_index, line in enumerate(selected_lines):
            count = expected_counts[line_index]
            line_timings = timings[cursor : cursor + count]
            mapping.append(
                AutoRepairLineMapping(
                    line_id=line.id,
                    syllable_start_index=cursor,
                    syllable_end_index=cursor + len(line_timings),
                )
            )
            cursor += count
            if not line_timings:
                warnings.append(f"Для строки '{line.text}' не осталось слогов.")
                continue

            old_words = words_by_line.get(line.id, [])
            old_syllables = syllables_by_line.get(line.id, [])
            remove_word_ids.extend(word.id for word in old_words)
            remove_syllable_ids.extend(syl.id for syl in old_syllables)

            word_slices = self._word_slices(line.text, line_timings, language)
            word_ids: list[str] = []
            for word_index, (word_text, syllable_slice) in enumerate(word_slices):
                word_id = self._stable_id("word", proposal_seed, line.id, str(word_index))
                word_ids.append(word_id)
                syllable_ids: list[str] = []
                for syl_index, timing in enumerate(syllable_slice):
                    syllable_id = self._stable_id(
                        "syl",
                        proposal_seed,
                        line.id,
                        str(word_index),
                        str(syl_index),
                    )
                    syllable_ids.append(syllable_id)
                    replace_syllables.append(
                        AlignmentSyllable(
                            id=syllable_id,
                            text=timing.syllable.lstrip("\n"),
                            start=timing.start,
                            end=timing.end,
                            word_id=word_id,
                            line_id=line.id,
                            flags=["realigned_syllables_fragment"],
                        )
                    )
                replace_words.append(
                    AlignmentWord(
                        id=word_id,
                        text=word_text,
                        start=syllable_slice[0].start,
                        end=syllable_slice[-1].end,
                        line_id=line.id,
                        syllable_ids=syllable_ids,
                        flags=["realigned_syllables_fragment"],
                    )
                )

            replace_lines.append(
                AlignmentLine(
                    id=line.id,
                    text=line.text,
                    start=line_timings[0].start,
                    end=line_timings[-1].end,
                    word_ids=word_ids,
                    flags=[
                        flag
                        for flag in line.flags
                        if flag not in {"needs_timing_review", "negative_duration", "overlap"}
                    ]
                    + ["realigned_syllables_fragment"],
                )
            )

        patch = AlignmentDocumentPatch(
            replace_lines=replace_lines,
            replace_words=replace_words,
            replace_syllables=replace_syllables,
            remove_word_ids=remove_word_ids,
            remove_syllable_ids=remove_syllable_ids,
        )
        return patch, mapping, warnings

    def _word_slices(
        self,
        text: str,
        timings: list[SyllableTiming],
        language: str,
    ) -> list[tuple[str, list[SyllableTiming]]]:
        words = text.split()
        if not words:
            return [(text, timings)]
        counts = [
            max(1, len(self.syllabifier.split_text_to_syllables(word, language)[0]))
            for word in words
        ]
        counts = self._fit_counts(counts, len(timings))
        result: list[tuple[str, list[SyllableTiming]]] = []
        cursor = 0
        for word, count in zip(words, counts, strict=False):
            chunk = timings[cursor : cursor + count]
            cursor += count
            if chunk:
                result.append((word, chunk))
        return result or [(text, timings)]

    @staticmethod
    def _fit_counts(counts: list[int], target: int) -> list[int]:
        if target <= 0:
            return [0 for _ in counts]
        counts = [max(1, count) for count in counts]
        while sum(counts) > target:
            index = max(range(len(counts)), key=lambda idx: counts[idx])
            if counts[index] <= 1:
                break
            counts[index] -= 1
        while sum(counts) < target:
            index = max(range(len(counts)), key=lambda idx: counts[idx])
            counts[index] += 1
        return counts

    def _score_candidate(
        self,
        selected_lines: list[AlignmentLine],
        timings: list[SyllableTiming],
        stats,
        candidate: _Candidate,
        patch_warnings: list[str],
    ) -> tuple[float, float, list[str], list[str], float, float, float, str]:
        warnings: list[str] = []
        reasons: list[str] = []
        query_text = " ".join(line.text for line in selected_lines)
        query_coverage, match_coverage = self._text_coverage(query_text, candidate.matched_text or "")
        if not timings:
            return 0.0, 0.0, reasons, ["critical:no_timings"], 1.0, query_coverage, match_coverage, "weak"

        duration = candidate.end - candidate.start
        syllable_rate = len(timings) / max(0.1, duration)
        duration_score = self._duration_plausibility(syllable_rate)
        fallback_ratio = (
            stats.proportional_fallback / max(1, stats.total_words)
            if getattr(stats, "total_words", 0)
            else 1.0
        )
        ctc_quality = max(0.0, 1.0 - fallback_ratio)
        monotonic = all(timings[idx].start <= timings[idx].end for idx in range(len(timings)))
        no_internal_overlap = all(
            timings[idx].start >= timings[idx - 1].end - 0.03
            for idx in range(1, len(timings))
        )
        structural = 1.0 if monotonic and no_internal_overlap else 0.15
        if not monotonic:
            warnings.append("critical:negative syllable duration")
        if not no_internal_overlap:
            warnings.append("critical:syllable overlap")

        old_duration = max(0.1, selected_lines[-1].end - selected_lines[0].start)
        boundary_shift = abs(candidate.start - selected_lines[0].start) + abs(
            candidate.end - selected_lines[-1].end
        )
        continuity = max(0.0, 1.0 - boundary_shift / max(2.0, old_duration * 2.0))

        mismatch_penalty = 0.08 if patch_warnings else 0.0
        score = (
            structural * 0.35
            + ctc_quality * 0.25
            + duration_score * 0.20
            + candidate.cheap_score * 0.10
            + continuity * 0.10
            - mismatch_penalty
        )
        score = max(0.0, min(1.0, score))
        confidence = max(0.0, min(1.0, score * (1.0 - fallback_ratio * 0.5)))
        if fallback_ratio >= 0.999:
            warnings.append("full_ctc_fallback")
            if candidate.evidence_level in {"vad", "grid", "current"}:
                score = min(score, 0.35)
                confidence = min(confidence, 0.20)
            else:
                score = min(score, 0.78)
                confidence = min(confidence, 0.35)
            if candidate.evidence_level in {"asr", "split_asr"} and query_coverage < 0.75:
                warnings.append("low_asr_query_coverage")
        elif fallback_ratio >= 0.67:
            warnings.append("high_ctc_fallback")
            score = min(score, 0.72 if candidate.evidence_level in {"asr", "split_asr"} else 0.50)
            confidence = min(confidence, 0.45)
        if candidate.evidence_level in {"vad", "grid", "current"}:
            warnings.append(f"weak_evidence:{candidate.evidence_level}")
            score = min(score, 0.60)
            confidence = min(confidence, 0.40)
        reasons.extend(
            [
                f"CTC fallback: {stats.proportional_fallback}/{max(1, stats.total_words)}",
                f"Слогов в секунду: {syllable_rate:.2f}",
            ]
        )
        if fallback_ratio < 0.35 and query_coverage >= 0.70:
            verification_level = "strong"
        elif candidate.evidence_level in {"asr", "split_asr"} and query_coverage >= 0.50:
            verification_level = "medium"
        else:
            verification_level = "weak"
        return (
            round(score, 4),
            round(confidence, 4),
            reasons,
            warnings,
            round(fallback_ratio, 4),
            round(query_coverage, 4),
            round(match_coverage, 4),
            verification_level,
        )

    @staticmethod
    def _duration_plausibility(syllable_rate: float) -> float:
        if 2.0 <= syllable_rate <= 7.5:
            return 1.0
        distance = min(abs(syllable_rate - 2.0), abs(syllable_rate - 7.5))
        return max(0.0, 1.0 - distance / 4.0)

    def _line_flags(
        self,
        document: AlignmentDocument,
        line: AlignmentLine,
        index: int,
    ) -> list[str]:
        flags = set(line.flags)
        syllables = [syl for syl in document.syllables if syl.line_id == line.id]
        if line.end <= line.start:
            flags.add("negative_duration")
        for syl_index, syllable in enumerate(syllables):
            syl_duration = syllable.end - syllable.start
            if syl_duration < 0.03:
                flags.add("too_short_syllable")
            if syl_duration > 2.5:
                flags.add("too_long_syllable")
            if syl_index > 0 and syllable.start < syllables[syl_index - 1].end:
                flags.add("overlap")
        if syllables and len(syllables) / max(0.1, line.end - line.start) > 8:
            flags.add("line_too_dense")
        if index > 0 and line.start < document.lines[index - 1].end - 0.03:
            flags.add("interline_overlap")
        return sorted(flags)

    @staticmethod
    def _root_cause_hints(flags: list[str], lines: list[AlignmentLine]) -> list[str]:
        hints: list[str] = []
        if any(flag in flags for flag in ("interline_overlap", "overlap", "negative_duration")):
            hints.append("manual_edit_artifact")
        if "line_too_dense" in flags:
            hints.append("monotonic_path_or_wrong_fragment")
        if "asr_evidence_far_from_current_timing" in flags:
            hints.append("asr_locator_disagrees_with_current_timing")
        if "global_suspect_line" in flags:
            hints.append("whole_document_suspect")
        if "too_long_syllable" in flags or "too_short_syllable" in flags:
            hints.append("acoustic_boundary_drift")
        if len(lines) > 3 and flags:
            hints.append("possible_forced_path_drift")
        return hints or ["unknown_alignment_anomaly"]

    @staticmethod
    def _document_end(document: AlignmentDocument) -> float:
        if not document.lines:
            return 0.0
        return max(line.end for line in document.lines)

    @staticmethod
    def _stable_id(prefix: str, *parts: str) -> str:
        return f"{prefix}_{uuid5(NAMESPACE_URL, ':'.join(parts)).hex[:12]}"

    @staticmethod
    def _range_key(start: float, end: float) -> str:
        return f"{round(start * 1000)}_{round(end * 1000)}"

    def _blocked_proposal(
        self,
        cluster: AutoRepairCluster,
        selected_lines: list[AlignmentLine],
        text: str,
        reason: str,
    ) -> AutoRepairProposal:
        return AutoRepairProposal(
            id=f"proposal_{cluster.id}_blocked",
            cluster_id=cluster.id,
            decision="blocked",
            root_cause_hints=cluster.root_cause_hints,
            score=0.0,
            confidence=0.0,
            margin=0.0,
            line_ids=cluster.line_ids,
            text=text,
            old_audio_range=cluster.old_audio_range,
            new_audio_range=cluster.old_audio_range,
            syllable_timings=[],
            document_patch=AlignmentDocumentPatch(),
            reasons=[reason],
            warnings=[reason],
        )

    @staticmethod
    def _summary(
        clusters: list[AutoRepairCluster],
        proposals: list[AutoRepairProposal],
    ) -> AutoRepairSummary:
        return AutoRepairSummary(
            clusters=len(clusters),
            auto_apply=sum(1 for proposal in proposals if proposal.decision == "auto_apply"),
            needs_review=sum(1 for proposal in proposals if proposal.decision == "needs_review"),
            rejected=sum(1 for proposal in proposals if proposal.decision == "rejected"),
            blocked=sum(1 for proposal in proposals if proposal.decision == "blocked"),
        )

    async def _create_auto_revision(
        self,
        base_revision: AlignmentRevision,
        proposals: list[AutoRepairProposal],
        job_id: str,
    ) -> str:
        if base_revision.document is None:
            raise RuntimeError("Base revision has no document")

        document = base_revision.document
        for proposal in proposals:
            document = self._apply_patch(document, proposal.document_patch)

        revision = AlignmentRevision(
            track_id=base_revision.track_id,
            revision_no=await self.repo.next_alignment_revision_no(base_revision.track_id),
            source="auto_repair",
            lyrics_text=lyrics_text_from_document(document),
            syllable_timings=document_to_syllable_timings(document),
            document=document,
            operations=[
                {
                    "type": "APPLY_ALIGNMENT_AUTO_REPAIR_SAFE",
                    "job_id": job_id,
                    "proposal_ids": [proposal.id for proposal in proposals],
                }
            ],
            diagnostics={"auto_repair_job_id": job_id},
            is_published=False,
        )
        stored = await self.repo.create_alignment_revision(revision)
        return stored.id

    @staticmethod
    def _apply_patch(
        document: AlignmentDocument,
        patch: AlignmentDocumentPatch,
    ) -> AlignmentDocument:
        replace_lines = {line.id: line for line in patch.replace_lines}
        replace_words = {word.id: word for word in patch.replace_words}
        replace_syllables = {syl.id: syl for syl in patch.replace_syllables}
        remove_word_ids = set(patch.remove_word_ids) | set(replace_words)
        remove_syllable_ids = set(patch.remove_syllable_ids) | set(replace_syllables)
        return AlignmentDocument(
            sections=document.sections,
            lines=[replace_lines.get(line.id, line) for line in document.lines],
            words=[word for word in document.words if word.id not in remove_word_ids]
            + list(replace_words.values()),
            syllables=[
                syl for syl in document.syllables if syl.id not in remove_syllable_ids
            ]
            + list(replace_syllables.values()),
        )
