"""Models for automated alignment repair jobs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from karaoke_shared.models.alignment import AlignmentLine, AlignmentSyllable, AlignmentWord
from karaoke_shared.models.track import SyllableTiming


AutoRepairMode = Literal["analyze_only", "propose", "auto_apply_safe"]
AutoRepairDecision = Literal["auto_apply", "needs_review", "rejected", "blocked"]


class AutoRepairAlignmentRequest(BaseModel):
    revision_id: str | None = None
    mode: AutoRepairMode = "propose"
    max_cluster_lines: int = Field(default=8, ge=1, le=24)
    max_audio_seconds: float = Field(default=60.0, ge=1.0, le=180.0)
    max_ctc_candidates: int = Field(default=72, ge=1, le=160)
    auto_apply_threshold: float = Field(default=0.90, ge=0.0, le=1.0)
    review_threshold: float = Field(default=0.72, ge=0.0, le=1.0)


class AutoRepairJobResponse(BaseModel):
    job_id: str


class AutoRepairRange(BaseModel):
    start: float
    end: float


class AutoRepairLineMapping(BaseModel):
    line_id: str
    syllable_start_index: int
    syllable_end_index: int


class AlignmentDocumentPatch(BaseModel):
    replace_lines: list[AlignmentLine] = Field(default_factory=list)
    replace_words: list[AlignmentWord] = Field(default_factory=list)
    replace_syllables: list[AlignmentSyllable] = Field(default_factory=list)
    remove_word_ids: list[str] = Field(default_factory=list)
    remove_syllable_ids: list[str] = Field(default_factory=list)


class AutoRepairCluster(BaseModel):
    id: str
    line_ids: list[str]
    start_line_index: int
    end_line_index: int
    old_audio_range: AutoRepairRange
    flags: list[str] = Field(default_factory=list)
    root_cause_hints: list[str] = Field(default_factory=list)


class AutoRepairProposal(BaseModel):
    id: str
    cluster_id: str
    decision: AutoRepairDecision
    root_cause_hints: list[str] = Field(default_factory=list)
    score: float
    confidence: float
    margin: float
    line_ids: list[str]
    text: str
    old_audio_range: AutoRepairRange
    new_audio_range: AutoRepairRange
    timing_origin: Literal["relative_to_fragment"] = "relative_to_fragment"
    syllable_timings: list[SyllableTiming] = Field(default_factory=list)
    line_mapping: list[AutoRepairLineMapping] | None = None
    document_patch: AlignmentDocumentPatch
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class AutoRepairSummary(BaseModel):
    clusters: int = 0
    auto_apply: int = 0
    needs_review: int = 0
    rejected: int = 0
    blocked: int = 0


class AutoRepairReport(BaseModel):
    job_id: str
    track_id: str
    base_revision_id: str
    source_audio_key: str
    status: Literal["ok", "partial", "failed"] = "ok"
    created_revision_id: str | None = None
    summary: AutoRepairSummary
    clusters: list[AutoRepairCluster] = Field(default_factory=list)
    proposals: list[AutoRepairProposal] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
