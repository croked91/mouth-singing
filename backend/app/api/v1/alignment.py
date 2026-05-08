"""Manual alignment editor API."""

from __future__ import annotations

import hmac
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, status
from karaoke_shared.alignment import (
    document_to_syllable_timings,
    lyrics_text_from_document,
    syllable_timings_to_document,
)
from karaoke_shared.models.alignment import AlignmentDocument, AlignmentRevision
from karaoke_shared.models.track import SyllableTiming
from karaoke_shared.repositories.pg_repository import PgRepository
from pydantic import BaseModel, Field

from app.config import settings
from app.dependencies import get_repo

router = APIRouter()


class AlignmentTrackSummary(BaseModel):
    id: str
    artist: str
    title: str
    duration_sec: int | None = None
    lyrics_source: str | None = None
    source: str
    status: str


class AlignmentEditorPayload(BaseModel):
    track: AlignmentTrackSummary
    stream_url: str | None = None
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


@router.get(
    "/tracks/{track_id}/alignment",
    response_model=AlignmentEditorPayload,
    summary="Open the manual alignment editor payload for a track",
)
async def get_track_alignment(
    track_id: str,
    repo: PgRepository = Depends(get_repo),
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

    return AlignmentEditorPayload(
        track=AlignmentTrackSummary(
            id=track.id,
            artist=track.artist,
            title=track.title,
            duration_sec=track.duration_sec,
            lyrics_source=track.lyrics_source,
            source=track.source,
            status=track.status,
        ),
        stream_url=(
            f"/api/v1/tracks/{track.id}/stream"
            if track.instrumental_key
            else None
        ),
        lyrics_text=(
            active_revision.lyrics_text if active_revision else track.lyrics_text
        ),
        syllable_timings=timings,
        document=document,
        active_revision=active_revision,
        revisions=revisions,
    )


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
