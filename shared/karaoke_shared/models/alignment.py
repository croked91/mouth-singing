"""Models used by the manual lyrics/alignment editor."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, Field

from karaoke_shared.models.track import SyllableTiming


class AlignmentSyllable(BaseModel):
    """Editable syllable node with stable identity."""

    id: str
    text: str
    start: float
    end: float
    word_id: str
    line_id: str
    flags: list[str] = Field(default_factory=list)


class AlignmentWord(BaseModel):
    """Editable word node grouping syllables."""

    id: str
    text: str
    start: float
    end: float
    line_id: str
    syllable_ids: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)


class AlignmentLine(BaseModel):
    """Editable lyric line node."""

    id: str
    text: str
    start: float
    end: float
    word_ids: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)


class AlignmentSection(BaseModel):
    """Optional section grouping for line-level/block operations."""

    id: str
    title: str | None = None
    line_ids: list[str] = Field(default_factory=list)


class AlignmentDocument(BaseModel):
    """Rich editor document compiled to flat ``SyllableTiming`` on publish."""

    sections: list[AlignmentSection] = Field(default_factory=list)
    lines: list[AlignmentLine] = Field(default_factory=list)
    words: list[AlignmentWord] = Field(default_factory=list)
    syllables: list[AlignmentSyllable] = Field(default_factory=list)


class AlignmentRevision(BaseModel):
    """Stored version of a track alignment."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    track_id: str
    revision_no: int
    source: str = "manual"
    lyrics_text: str | None = None
    syllable_timings: list[SyllableTiming] = Field(default_factory=list)
    document: AlignmentDocument | None = None
    operations: list[dict] = Field(default_factory=list)
    diagnostics: dict = Field(default_factory=dict)
    is_published: bool = False
    created_by: str | None = None
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    published_at: str | None = None
