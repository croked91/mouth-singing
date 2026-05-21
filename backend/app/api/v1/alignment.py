"""Manual alignment editor API."""

from __future__ import annotations

import hmac
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from karaoke_shared.alignment import (
    document_to_syllable_timings,
    lyrics_text_from_document,
    syllable_timings_to_document,
)
from karaoke_shared.models.alignment import AlignmentDocument, AlignmentRevision
from karaoke_shared.models.auto_repair import (
    AlignmentDocumentPatch,
    AutoRepairAlignmentRequest,
    AutoRepairJobResponse,
    AutoRepairProposal,
    AutoRepairReport,
)
from karaoke_shared.models.job import JobCreate
from karaoke_shared.models.track import SyllableTiming
from karaoke_shared.models.track import TrackUpdate
from karaoke_shared.messaging import RabbitMQClient
from karaoke_shared.repositories.pg_repository import PgRepository
from karaoke_shared.storage import S3Storage
from pydantic import BaseModel, Field

from app.config import settings
from app.dependencies import get_repo, get_storage

router = APIRouter()


class AlignmentTrackSummary(BaseModel):
    id: str
    artist: str
    title: str
    duration_sec: int | None = None
    lyrics_source: str | None = None
    review_vocal_key: str | None = None
    source: str
    status: str
    alignment_review_status: str = "pending"
    review_requested_at: str | None = None
    review_completed_at: str | None = None


class AlignmentReviewQueueItem(BaseModel):
    id: str
    artist: str
    title: str
    duration_sec: int | None = None
    lyrics_source: str | None = None
    alignment_review_status: str = "pending"
    review_requested_at: str | None = None
    source: str


class AlignmentEditorPayload(BaseModel):
    track: AlignmentTrackSummary
    stream_url: str | None = None
    stream_source: str = "instrumental"
    lyrics_text: str | None = None
    syllable_timings: list[SyllableTiming] = Field(default_factory=list)
    document: AlignmentDocument
    active_revision: AlignmentRevision | None = None
    revisions: list[AlignmentRevision] = Field(default_factory=list)


class SaveAlignmentDraftRequest(BaseModel):
    document: AlignmentDocument
    operations: list[dict] = Field(default_factory=list)
    diagnostics: dict = Field(default_factory=dict)
    created_by: str | None = None


class SaveAlignmentDraftResponse(BaseModel):
    revision: AlignmentRevision


class PublishAlignmentRequest(BaseModel):
    revision_id: str


class PublishAlignmentResponse(BaseModel):
    revision: AlignmentRevision


class RestoreAlignmentResponse(BaseModel):
    revision: AlignmentRevision


class ApplyAutoRepairRequest(BaseModel):
    job_id: str
    base_revision_id: str
    proposal_ids: list[str]
    created_by: str | None = None


class ApplyAutoRepairResponse(BaseModel):
    revision: AlignmentRevision


class RealignSyllablesFragmentRequest(BaseModel):
    audio_start: float
    audio_end: float
    line_ids: list[str] = Field(default_factory=list)
    text: str
    preserve_line_breaks: bool = True


class RealignSyllablesFragmentJobResponse(BaseModel):
    job_id: str


def _require_admin_secret(x_admin_secret: str | None) -> None:
    if not hmac.compare_digest(x_admin_secret or "", settings.admin_secret):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing admin secret.",
        )


async def _ensure_initial_revision(
    track_id: str,
    repo: PgRepository,
) -> AlignmentRevision | None:
    revisions = await repo.list_alignment_revisions(track_id)
    if revisions:
        return next(
            (revision for revision in revisions if revision.is_published),
            revisions[0],
        )

    track = await repo.get_track(track_id)
    if track is None:
        return None
    timings = track.syllable_timings or []
    document = syllable_timings_to_document(timings)
    revision = AlignmentRevision(
        id=str(uuid4()),
        track_id=track.id,
        revision_no=1,
        source="auto",
        lyrics_text=track.lyrics_text,
        syllable_timings=timings,
        document=document,
        is_published=True,
        published_at=track.updated_at,
    )
    return await repo.create_alignment_revision(revision)


def _extract_review_vocal_key(data: dict | None) -> str | None:
    if not data:
        return None
    candidate = (
        data.get("review_vocal_key")
        or data.get("audio_key")
    )
    if candidate and str(candidate).startswith("review-vocals/"):
        return str(candidate)
    return None


async def _resolve_review_vocal_key(
    track_id: str,
    repo: PgRepository,
    storage: S3Storage,
) -> str | None:
    """Return the best stored vocal stem key for repair jobs."""
    track = await repo.get_track(track_id)
    if track is None:
        return None
    if track.review_vocal_key:
        if await storage.exists(track.review_vocal_key):
            return track.review_vocal_key

    for job in await repo.list_completed_jobs_for_track(track_id):
        candidate = _extract_review_vocal_key(job.result) or _extract_review_vocal_key(job.data)
        if candidate and await storage.exists(candidate):
            await repo.update_track(track_id, TrackUpdate(review_vocal_key=candidate))
            return candidate
    return None


def _apply_document_patch(
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
        words=[
            word
            for word in document.words
            if word.id not in remove_word_ids
        ] + list(replace_words.values()),
        syllables=[
            syl
            for syl in document.syllables
            if syl.id not in remove_syllable_ids
        ] + list(replace_syllables.values()),
    )


def _proposal_by_id(report: AutoRepairReport) -> dict[str, AutoRepairProposal]:
    return {proposal.id: proposal for proposal in report.proposals}


async def _get_rmq_or_reconnect(request: Request) -> RabbitMQClient | None:
    """Return RabbitMQ client, reconnecting if backend started before broker."""
    rmq = getattr(request.app.state, "rmq", None)
    if rmq is not None:
        return rmq

    rmq = RabbitMQClient(settings.rabbitmq_url)
    try:
        await rmq.connect()
        await rmq.declare_topology()
    except Exception:
        await rmq.close()
        return None
    request.app.state.rmq = rmq
    return rmq


@router.get(
    "/tracks/alignment-reviews",
    response_model=list[AlignmentReviewQueueItem],
    summary="List tracks available for human alignment review",
)
async def list_alignment_review_queue(
    status: str = "pending",
    limit: int = 50,
    repo: PgRepository = Depends(get_repo),
) -> list[AlignmentReviewQueueItem]:
    """Return recent ready tracks for the admin manual alignment shortcut.

    This path must stay before ``/tracks/{track_id}/alignment`` because
    otherwise FastAPI treats ``alignment-reviews`` as a track id.
    Older databases do not have dedicated review-status columns, so the queue
    is derived from ready tracks that have lyrics/timings and can be opened in
    the manual editor.
    """
    del status
    rows = await repo.pool.fetch(
        """
        SELECT *
        FROM tracks
        WHERE status = 'ready'
          AND (lyrics_text IS NOT NULL OR syllable_timings IS NOT NULL)
        ORDER BY updated_at DESC
        LIMIT $1
        """,
        max(1, min(limit, 100)),
    )
    return [
        AlignmentReviewQueueItem(
            id=row["id"],
            artist=row["artist"],
            title=row["title"],
            duration_sec=row.get("duration_sec"),
            lyrics_source=row.get("lyrics_source"),
            alignment_review_status="pending",
            review_requested_at=(
                row["updated_at"].isoformat()
                if hasattr(row.get("updated_at"), "isoformat")
                else str(row.get("updated_at")) if row.get("updated_at") else None
            ),
            source=row["source"],
        )
        for row in rows
    ]


@router.get(
    "/tracks/{track_id}/alignment",
    response_model=AlignmentEditorPayload,
    summary="Open the manual alignment editor payload for a track",
)
async def get_track_alignment(
    track_id: str,
    repo: PgRepository = Depends(get_repo),
    storage: S3Storage = Depends(get_storage),
) -> AlignmentEditorPayload:
    track = await repo.get_track(track_id)
    if track is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Track '{track_id}' not found.",
        )

    active_revision = await _ensure_initial_revision(track_id, repo)
    revisions = await repo.list_alignment_revisions(track_id)
    timings = (
        active_revision.syllable_timings
        if active_revision
        else (track.syllable_timings or [])
    )
    document = (
        active_revision.document
        if active_revision and active_revision.document
        else syllable_timings_to_document(timings)
    )
    review_vocal_key = await _resolve_review_vocal_key(track_id, repo, storage)
    stream_source = "vocals" if review_vocal_key else "instrumental"

    return AlignmentEditorPayload(
        track=AlignmentTrackSummary(
            id=track.id,
            artist=track.artist,
            title=track.title,
            duration_sec=track.duration_sec,
            lyrics_source=track.lyrics_source,
            review_vocal_key=review_vocal_key,
            source=track.source,
            status=track.status,
        ),
        stream_url=(
            f"/api/v1/tracks/{track.id}/review-vocals/stream"
            if review_vocal_key
            else f"/api/v1/tracks/{track.id}/stream"
            if track.instrumental_key
            else None
        ),
        stream_source=stream_source,
        lyrics_text=(
            active_revision.lyrics_text if active_revision else track.lyrics_text
        ),
        syllable_timings=timings,
        document=document,
        active_revision=active_revision,
        revisions=revisions,
    )


@router.post(
    "/tracks/{track_id}/alignment/auto-repair",
    response_model=AutoRepairJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start an asynchronous automatic alignment repair job",
)
async def start_alignment_auto_repair(
    track_id: str,
    payload: AutoRepairAlignmentRequest,
    request: Request,
    x_admin_secret: str | None = Header(default=None),
    repo: PgRepository = Depends(get_repo),
) -> AutoRepairJobResponse:
    _require_admin_secret(x_admin_secret)
    track = await repo.get_track(track_id)
    if track is None:
        raise HTTPException(status_code=404, detail=f"Track '{track_id}' not found.")

    revision = None
    if payload.revision_id:
        revision = await repo.get_alignment_revision(payload.revision_id)
        if revision is None or revision.track_id != track_id:
            raise HTTPException(status_code=404, detail="Alignment revision not found.")
    else:
        revision = await _ensure_initial_revision(track_id, repo)
    if revision is None:
        raise HTTPException(status_code=404, detail="Alignment revision not found.")

    review_vocal_key = await _resolve_review_vocal_key(
        track_id,
        repo,
        request.app.state.storage,
    )
    if not review_vocal_key:
        raise HTTPException(
            status_code=409,
            detail=(
                "Для автоисправления нет vocal stem. "
                "Нужно переобработать трек или сохранить review-vocals объект."
            ),
        )

    rmq = await _get_rmq_or_reconnect(request)
    if rmq is None:
        raise HTTPException(status_code=503, detail="Job queue is unavailable.")

    job = await repo.create_job(
        JobCreate(
            track_id=track_id,
            mp3_key=review_vocal_key,
            priority=7,
            max_attempts=1,
            data={
                "task": "alignment_auto_repair",
                "track_id": track_id,
                "revision_id": revision.id,
                "audio_key": review_vocal_key,
                **payload.model_dump(),
            },
        )
    )
    await rmq.publish(
        "jobs",
        "",
        {"job_id": job.id, "mp3_key": review_vocal_key},
        priority=job.priority,
    )
    return AutoRepairJobResponse(job_id=job.id)


@router.post(
    "/tracks/{track_id}/alignment/realign-syllables-fragment",
    response_model=RealignSyllablesFragmentJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start asynchronous syllable realignment for a manually selected fragment",
)
async def realign_syllables_fragment(
    track_id: str,
    payload: RealignSyllablesFragmentRequest,
    request: Request,
    x_admin_secret: str | None = Header(default=None),
    repo: PgRepository = Depends(get_repo),
) -> RealignSyllablesFragmentJobResponse:
    _require_admin_secret(x_admin_secret)
    track = await repo.get_track(track_id)
    if track is None:
        raise HTTPException(status_code=404, detail=f"Track '{track_id}' not found.")

    duration = float(track.duration_sec or 0)
    if payload.audio_start < 0:
        raise HTTPException(status_code=422, detail="audio_start must be >= 0.")
    if payload.audio_end <= payload.audio_start:
        raise HTTPException(status_code=422, detail="audio_end must be after audio_start.")
    if payload.audio_end - payload.audio_start < 0.3:
        raise HTTPException(status_code=422, detail="Audio fragment is too short.")
    if payload.audio_end - payload.audio_start > 60:
        raise HTTPException(status_code=422, detail="Audio fragment must be at most 60 seconds.")
    if duration and payload.audio_end > duration + 0.5:
        raise HTTPException(status_code=422, detail="audio_end is outside the track duration.")
    if not payload.text.strip():
        raise HTTPException(status_code=422, detail="Text is empty.")

    review_vocal_key = await _resolve_review_vocal_key(
        track_id,
        repo,
        request.app.state.storage,
    )
    if not review_vocal_key:
        raise HTTPException(
            status_code=409,
            detail="Для выравнивания нет vocal stem. Нужно переобработать трек.",
        )

    rmq = await _get_rmq_or_reconnect(request)
    if rmq is None:
        raise HTTPException(status_code=503, detail="Job queue is unavailable.")

    job = await repo.create_job(
        JobCreate(
            track_id=track_id,
            mp3_key=review_vocal_key,
            priority=8,
            max_attempts=1,
            data={
                "task": "alignment_fragment_realign",
                "track_id": track_id,
                "audio_key": review_vocal_key,
                "audio_start": payload.audio_start,
                "audio_end": payload.audio_end,
                "line_ids": payload.line_ids,
                "text": payload.text,
                "preserve_line_breaks": payload.preserve_line_breaks,
            },
        )
    )
    await rmq.publish(
        "jobs",
        "",
        {"job_id": job.id, "mp3_key": review_vocal_key},
        priority=job.priority,
    )
    return RealignSyllablesFragmentJobResponse(job_id=job.id)


@router.post(
    "/tracks/{track_id}/alignment/auto-repair/apply",
    response_model=ApplyAutoRepairResponse,
    summary="Apply selected auto-repair proposals as a new draft revision",
)
async def apply_alignment_auto_repair(
    track_id: str,
    payload: ApplyAutoRepairRequest,
    x_admin_secret: str | None = Header(default=None),
    repo: PgRepository = Depends(get_repo),
) -> ApplyAutoRepairResponse:
    _require_admin_secret(x_admin_secret)
    track = await repo.get_track(track_id)
    if track is None:
        raise HTTPException(status_code=404, detail=f"Track '{track_id}' not found.")

    base_revision = await repo.get_alignment_revision(payload.base_revision_id)
    if base_revision is None or base_revision.track_id != track_id:
        raise HTTPException(status_code=404, detail="Base alignment revision not found.")
    if base_revision.document is None:
        raise HTTPException(status_code=409, detail="Base revision has no document.")

    job = await repo.get_job(payload.job_id)
    if job is None or job.track_id != track_id or not job.result:
        raise HTTPException(status_code=404, detail="Auto-repair job result not found.")

    report = AutoRepairReport(**job.result)
    if report.base_revision_id != payload.base_revision_id:
        raise HTTPException(
            status_code=409,
            detail="Auto-repair report was produced for another base revision.",
        )

    proposals = _proposal_by_id(report)
    missing = [proposal_id for proposal_id in payload.proposal_ids if proposal_id not in proposals]
    if missing:
        raise HTTPException(status_code=400, detail=f"Unknown proposal ids: {missing}")

    document = base_revision.document
    applied: list[str] = []
    for proposal_id in payload.proposal_ids:
        proposal = proposals[proposal_id]
        if proposal.decision == "blocked":
            raise HTTPException(
                status_code=409,
                detail=f"Proposal '{proposal_id}' is blocked and cannot be applied.",
            )
        document = _apply_document_patch(document, proposal.document_patch)
        applied.append(proposal_id)

    revision = AlignmentRevision(
        track_id=track_id,
        revision_no=await repo.next_alignment_revision_no(track_id),
        source="auto_repair",
        lyrics_text=lyrics_text_from_document(document),
        syllable_timings=document_to_syllable_timings(document),
        document=document,
        operations=[
            {
                "type": "APPLY_ALIGNMENT_AUTO_REPAIR",
                "job_id": payload.job_id,
                "proposal_ids": applied,
            }
        ],
        diagnostics={"auto_repair_job_id": payload.job_id},
        is_published=False,
        created_by=payload.created_by,
    )
    return ApplyAutoRepairResponse(revision=await repo.create_alignment_revision(revision))


@router.put(
    "/tracks/{track_id}/alignment/draft",
    response_model=SaveAlignmentDraftResponse,
    summary="Save an unpublished manual alignment draft",
)
async def save_alignment_draft(
    track_id: str,
    payload: SaveAlignmentDraftRequest,
    x_admin_secret: str | None = Header(default=None),
    repo: PgRepository = Depends(get_repo),
) -> SaveAlignmentDraftResponse:
    _require_admin_secret(x_admin_secret)
    track = await repo.get_track(track_id)
    if track is None:
        raise HTTPException(status_code=404, detail=f"Track '{track_id}' not found.")

    timings = document_to_syllable_timings(payload.document)
    revision = AlignmentRevision(
        track_id=track_id,
        revision_no=await repo.next_alignment_revision_no(track_id),
        source="manual",
        lyrics_text=lyrics_text_from_document(payload.document),
        syllable_timings=timings,
        document=payload.document,
        operations=payload.operations,
        diagnostics=payload.diagnostics,
        is_published=False,
        created_by=payload.created_by,
    )
    stored = await repo.create_alignment_revision(revision)
    return SaveAlignmentDraftResponse(revision=stored)


@router.post(
    "/tracks/{track_id}/alignment/publish",
    response_model=PublishAlignmentResponse,
    summary="Publish an alignment revision to the track snapshot",
)
async def publish_alignment(
    track_id: str,
    payload: PublishAlignmentRequest,
    x_admin_secret: str | None = Header(default=None),
    repo: PgRepository = Depends(get_repo),
) -> PublishAlignmentResponse:
    _require_admin_secret(x_admin_secret)
    revision = await repo.publish_alignment_revision(track_id, payload.revision_id)
    if revision is None:
        raise HTTPException(status_code=404, detail="Alignment revision not found.")
    return PublishAlignmentResponse(revision=revision)


@router.get(
    "/tracks/{track_id}/alignment/revisions",
    response_model=list[AlignmentRevision],
    summary="List alignment revisions for a track",
)
async def list_alignment_revisions(
    track_id: str,
    repo: PgRepository = Depends(get_repo),
) -> list[AlignmentRevision]:
    await _ensure_initial_revision(track_id, repo)
    return await repo.list_alignment_revisions(track_id)


@router.post(
    "/tracks/{track_id}/alignment/revisions/{revision_id}/restore",
    response_model=RestoreAlignmentResponse,
    summary="Restore a revision by publishing it",
)
async def restore_alignment_revision(
    track_id: str,
    revision_id: str,
    x_admin_secret: str | None = Header(default=None),
    repo: PgRepository = Depends(get_repo),
) -> RestoreAlignmentResponse:
    _require_admin_secret(x_admin_secret)
    revision = await repo.publish_alignment_revision(track_id, revision_id)
    if revision is None:
        raise HTTPException(status_code=404, detail="Alignment revision not found.")
    return RestoreAlignmentResponse(revision=revision)
