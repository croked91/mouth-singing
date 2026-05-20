"""Automated repair of problematic alignment fragments.

The worker owns all heavy alignment work.  Backend only enqueues jobs and
stores/apply reports.
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
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


class AlignmentAutoRepairEngine:
    """Find and propose repairs for alignment anomalies."""

    def __init__(
        self,
        job_service: JobService,
        repo: PgRepository,
        storage: S3Storage,
        vad_processor,
        ctc_aligner,
    ) -> None:
        self.job_service = job_service
        self.repo = repo
        self.storage = storage
        self.vad_processor = vad_processor
        self.ctc_aligner = ctc_aligner
        self.syllabifier = Syllabifier()

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
                clusters = self._build_clusters(revision.document, duration)

                proposals: list[AutoRepairProposal] = []
                for index, cluster in enumerate(clusters):
                    progress = 20 + int((index / max(1, len(clusters))) * 65)
                    await self.job_service.mark_step(
                        job.id,
                        f"auto_repair_cluster_{index + 1}_of_{len(clusters)}",
                        progress,
                    )
                    proposals.extend(
                        await self._repair_cluster(
                            job_id=job.id,
                            document=revision.document,
                            cluster=cluster,
                            audio_path=audio_path,
                            vad_segments=vad_segments,
                            duration=duration,
                            language=language,
                            config=data,
                        )
                    )

            report = AutoRepairReport(
                job_id=job.id,
                track_id=track_id,
                base_revision_id=revision.id,
                source_audio_key=audio_key,
                status="ok" if proposals or not clusters else "partial",
                summary=self._summary(clusters, proposals),
                clusters=clusters,
                proposals=proposals,
                warnings=[] if clusters else ["Проблемные участки не найдены."],
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

    async def _safe_vad(self, audio_path: str) -> list[tuple[float, float]]:
        try:
            result = await asyncio.to_thread(self.vad_processor.process, audio_path)
            return list(result.segments or [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("auto_repair_vad_failed", error=str(exc))
            return []

    def _build_clusters(
        self,
        document: AlignmentDocument,
        duration: float,
    ) -> list[AutoRepairCluster]:
        line_flags = [
            self._line_flags(document, line, index)
            for index, line in enumerate(document.lines)
        ]
        bad_indices = [index for index, flags in enumerate(line_flags) if flags]
        if not bad_indices:
            return []

        clusters: list[AutoRepairCluster] = []
        start = prev = bad_indices[0]
        for index in bad_indices[1:]:
            if index == prev + 1:
                prev = index
                continue
            clusters.append(self._make_cluster(document, line_flags, start, prev, duration))
            start = prev = index
        clusters.append(self._make_cluster(document, line_flags, start, prev, duration))
        return clusters

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

    async def _repair_cluster(
        self,
        job_id: str,
        document: AlignmentDocument,
        cluster: AutoRepairCluster,
        audio_path: str,
        vad_segments: list[tuple[float, float]],
        duration: float,
        language: str,
        config: dict,
    ) -> list[AutoRepairProposal]:
        selected_lines = [
            line for line in document.lines if line.id in set(cluster.line_ids)
        ]
        if not selected_lines:
            return []
        text = "\n".join(line.text for line in selected_lines if line.text.strip())
        if not text.strip():
            return []

        candidates = self._generate_candidates(
            cluster,
            selected_lines,
            vad_segments,
            duration,
            language,
            config,
        )
        scored: list[_ScoredCandidate] = []
        for candidate in candidates[: int(config.get("max_ctc_candidates", 24))]:
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

        if not scored:
            return [
                self._blocked_proposal(cluster, selected_lines, text, "Все варианты выравнивания завершились ошибкой.")
            ]

        scored.sort(key=lambda item: item.score, reverse=True)
        best = scored[0]
        second_score = scored[1].score if len(scored) > 1 else 0.0
        margin = max(0.0, best.score - second_score)
        auto_threshold = float(config.get("auto_apply_threshold", 0.90))
        review_threshold = float(config.get("review_threshold", 0.72))
        critical_warnings = any("critical" in warning for warning in best.warnings)
        if best.score >= auto_threshold and margin >= 0.10 and not critical_warnings:
            decision = "auto_apply"
        elif best.score >= review_threshold:
            decision = "needs_review"
        else:
            decision = "rejected"

        proposal = AutoRepairProposal(
            id=f"proposal_{cluster.id}_{self._range_key(best.candidate.start, best.candidate.end)}",
            cluster_id=cluster.id,
            decision=decision,
            root_cause_hints=cluster.root_cause_hints,
            score=round(best.score, 4),
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
        )
        return [proposal]

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
        score, confidence, reasons, score_warnings = self._score_candidate(
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
        duration: float,
        language: str,
        config: dict,
    ) -> list[_Candidate]:
        old_start = cluster.old_audio_range.start
        old_end = cluster.old_audio_range.end
        max_audio_seconds = float(config.get("max_audio_seconds", 60.0))
        raw: list[_Candidate] = []

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

        search_start = max(0.0, old_start - 8.0)
        search_end = min(duration, old_end + 8.0)
        nearby_vad = [
            (start, end)
            for start, end in vad_segments
            if end >= search_start and start <= search_end
        ]
        for start, end in nearby_vad:
            raw.append(self._candidate(start - 0.2, end + 0.2, duration, "VAD phrase boundary"))
        if nearby_vad:
            raw.append(
                self._candidate(
                    min(start for start, _ in nearby_vad) - 0.2,
                    max(end for _, end in nearby_vad) + 0.2,
                    duration,
                    "merged VAD region",
                )
            )

        expected_syllables = sum(
            len(self.syllabifier.split_text_to_syllables(line.text, language)[0])
            for line in selected_lines
        )

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
            cheap_score = candidate.cheap_score + self._duration_plausibility(rate) * 0.4
            candidate = _Candidate(candidate.start, candidate.end, cheap_score, candidate.reasons)
            prev = dedup.get(key)
            if prev is None or candidate.cheap_score > prev.cheap_score:
                dedup[key] = candidate

        return sorted(dedup.values(), key=lambda item: item.cheap_score, reverse=True)

    @staticmethod
    def _candidate(start: float, end: float, duration: float, reason: str) -> _Candidate:
        start = max(0.0, min(duration, start))
        end = max(0.0, min(duration, end))
        return _Candidate(start=start, end=end, cheap_score=0.5, reasons=(reason,))

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
    ) -> tuple[float, float, list[str], list[str]]:
        warnings: list[str] = []
        reasons: list[str] = []
        if not timings:
            return 0.0, 0.0, reasons, ["critical:no_timings"]

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
        reasons.extend(
            [
                f"CTC fallback: {stats.proportional_fallback}/{max(1, stats.total_words)}",
                f"Слогов в секунду: {syllable_rate:.2f}",
            ]
        )
        return score, confidence, reasons, warnings

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
