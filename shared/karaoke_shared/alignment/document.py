"""Conversion helpers between flat player timings and editable documents."""

from __future__ import annotations

import re
from uuid import uuid4

from karaoke_shared.models.alignment import (
    AlignmentDocument,
    AlignmentLine,
    AlignmentSection,
    AlignmentSyllable,
    AlignmentWord,
)
from karaoke_shared.models.track import SyllableTiming

_WORD_RE = re.compile(r"\S+")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def _split_lines(timings: list[SyllableTiming]) -> list[list[SyllableTiming]]:
    lines: list[list[SyllableTiming]] = []
    current: list[SyllableTiming] = []
    for timing in timings:
        text = timing.syllable
        starts_new = text.startswith("\n")
        if starts_new and current:
            lines.append(current)
            current = []
        current.append(
            SyllableTiming(
                syllable=text[1:] if starts_new else text,
                start=timing.start,
                end=timing.end,
            )
        )
    if current:
        lines.append(current)
    return lines


def syllable_timings_to_document(
    timings: list[SyllableTiming] | None,
) -> AlignmentDocument:
    """Build an editor document from player-compatible syllable timings."""
    if not timings:
        section_id = _new_id("section")
        return AlignmentDocument(
            sections=[AlignmentSection(id=section_id, title="Main", line_ids=[])]
        )

    section_id = _new_id("section")
    line_ids: list[str] = []
    lines: list[AlignmentLine] = []
    words: list[AlignmentWord] = []
    syllables: list[AlignmentSyllable] = []

    for line_timings in _split_lines(timings):
        line_id = _new_id("line")
        word_id = _new_id("word")
        line_ids.append(line_id)
        word_ids = [word_id]
        line_text = "".join(t.syllable for t in line_timings).strip()
        start = line_timings[0].start
        end = line_timings[-1].end
        syllable_ids: list[str] = []

        for timing in line_timings:
            syllable_id = _new_id("syl")
            syllable_ids.append(syllable_id)
            syllables.append(
                AlignmentSyllable(
                    id=syllable_id,
                    text=timing.syllable,
                    start=timing.start,
                    end=timing.end,
                    word_id=word_id,
                    line_id=line_id,
                )
            )

        words.append(
            AlignmentWord(
                id=word_id,
                text=line_text,
                start=start,
                end=end,
                line_id=line_id,
                syllable_ids=syllable_ids,
            )
        )
        lines.append(
            AlignmentLine(
                id=line_id,
                text=line_text,
                start=start,
                end=end,
                word_ids=word_ids,
            )
        )

    return AlignmentDocument(
        sections=[AlignmentSection(id=section_id, title="Main", line_ids=line_ids)],
        lines=lines,
        words=words,
        syllables=syllables,
    )


def document_to_syllable_timings(document: AlignmentDocument) -> list[SyllableTiming]:
    """Compile an editor document into player-compatible flat timings."""
    words_by_id = {word.id: word for word in document.words}
    syllables_by_id = {syl.id: syl for syl in document.syllables}
    result: list[SyllableTiming] = []

    for line_index, line in enumerate(document.lines):
        line_syllables: list[AlignmentSyllable] = []
        for word_id in line.word_ids:
            word = words_by_id.get(word_id)
            if word is None:
                continue
            for syllable_id in word.syllable_ids:
                syllable = syllables_by_id.get(syllable_id)
                if syllable is not None:
                    line_syllables.append(syllable)

        if not line_syllables and line.text.strip():
            line_syllables = _line_text_to_syllables(line)

        for syllable_index, syllable in enumerate(line_syllables):
            text = syllable.text
            if line_index > 0 and syllable_index == 0:
                text = f"\n{text}"
            result.append(
                SyllableTiming(
                    syllable=text,
                    start=max(0.0, syllable.start),
                    end=max(syllable.start + 0.01, syllable.end),
                )
            )
    return result


def lyrics_text_from_document(document: AlignmentDocument) -> str:
    """Return plain lyrics text from an editor document."""
    return "\n".join(line.text for line in document.lines if line.text.strip())


def _line_text_to_syllables(line: AlignmentLine) -> list[AlignmentSyllable]:
    matches = list(_WORD_RE.finditer(line.text))
    if not matches:
        return []
    duration = max(0.01, line.end - line.start)
    total_chars = sum(max(1, len(m.group(0))) for m in matches)
    cursor = line.start
    syllables: list[AlignmentSyllable] = []
    word_id = line.word_ids[0] if line.word_ids else _new_id("word")
    for idx, match in enumerate(matches):
        text = match.group(0)
        share = max(1, len(text)) / total_chars
        end = line.end if idx == len(matches) - 1 else cursor + duration * share
        syllables.append(
            AlignmentSyllable(
                id=_new_id("syl"),
                text=text,
                start=cursor,
                end=end,
                word_id=word_id,
                line_id=line.id,
            )
        )
        cursor = end
    return syllables
