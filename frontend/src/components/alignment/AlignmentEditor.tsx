import React, { useMemo, useRef, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Chip,
  Divider,
  IconButton,
  Paper,
  Stack,
  TextField,
  Typography,
} from '@mui/material';
import AddIcon from '@mui/icons-material/Add';
import ContentCopyIcon from '@mui/icons-material/ContentCopy';
import DeleteIcon from '@mui/icons-material/Delete';
import KeyboardArrowDownIcon from '@mui/icons-material/KeyboardArrowDown';
import KeyboardArrowUpIcon from '@mui/icons-material/KeyboardArrowUp';
import PauseIcon from '@mui/icons-material/Pause';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';
import SaveIcon from '@mui/icons-material/Save';
import UploadIcon from '@mui/icons-material/Upload';
import type {
  AlignmentDocument,
  AlignmentEditorPayload,
  AlignmentLine,
  AlignmentRevision,
  AlignmentSyllable,
  AlignmentWord,
} from '../../types';

interface AlignmentEditorProps {
  payload: AlignmentEditorPayload;
  adminSecret: string;
  onSaveDraft: (document: AlignmentDocument) => Promise<AlignmentRevision>;
  onPublish: (revisionId: string) => Promise<AlignmentRevision>;
}

const NUDGE_SMALL = 0.05;
const NUDGE_LARGE = 0.25;

function makeId(prefix: string): string {
  return `${prefix}_${Math.random().toString(36).slice(2, 10)}`;
}

function formatTime(value: number): string {
  if (!Number.isFinite(value)) return '0.000';
  return value.toFixed(3);
}

function cloneDocument(document: AlignmentDocument): AlignmentDocument {
  return JSON.parse(JSON.stringify(document)) as AlignmentDocument;
}

function lineDuration(line: AlignmentLine): number {
  return Math.max(0, line.end - line.start);
}

function getLineSyllables(document: AlignmentDocument, line: AlignmentLine): AlignmentSyllable[] {
  const wordsById = new Map(document.words.map((word) => [word.id, word]));
  const syllablesById = new Map(document.syllables.map((syllable) => [syllable.id, syllable]));
  return line.word_ids.flatMap((wordId) => {
    const word = wordsById.get(wordId);
    if (!word) return [];
    return word.syllable_ids
      .map((syllableId) => syllablesById.get(syllableId))
      .filter((syllable): syllable is AlignmentSyllable => Boolean(syllable));
  });
}

function rebuildLineFromText(line: AlignmentLine, text: string): {
  line: AlignmentLine;
  words: AlignmentWord[];
  syllables: AlignmentSyllable[];
} {
  const tokens = text.trim().split(/\s+/).filter(Boolean);
  const start = line.start;
  const end = Math.max(line.start + 0.1, line.end);
  const duration = end - start;
  const totalChars = Math.max(1, tokens.reduce((sum, token) => sum + token.length, 0));
  let cursor = start;
  const wordIds: string[] = [];
  const words: AlignmentWord[] = [];
  const syllables: AlignmentSyllable[] = [];

  tokens.forEach((token, index) => {
    const wordId = makeId('word');
    const syllableId = makeId('syl');
    const tokenDuration = index === tokens.length - 1 ? end - cursor : duration * (token.length / totalChars);
    const tokenEnd = index === tokens.length - 1 ? end : cursor + tokenDuration;
    wordIds.push(wordId);
    words.push({
      id: wordId,
      text: token,
      start: cursor,
      end: tokenEnd,
      line_id: line.id,
      syllable_ids: [syllableId],
      flags: ['needs_timing_review'],
    });
    syllables.push({
      id: syllableId,
      text: token,
      start: cursor,
      end: tokenEnd,
      word_id: wordId,
      line_id: line.id,
      flags: ['needs_timing_review'],
    });
    cursor = tokenEnd;
  });

  return {
    line: {
      ...line,
      text,
      start,
      end,
      word_ids: wordIds,
      flags: Array.from(new Set([...line.flags, 'needs_timing_review'])),
    },
    words,
    syllables,
  };
}

function shiftLine(document: AlignmentDocument, lineId: string, delta: number): AlignmentDocument {
  const next = cloneDocument(document);
  next.lines = next.lines.map((line) => (
    line.id === lineId ? { ...line, start: Math.max(0, line.start + delta), end: Math.max(0.01, line.end + delta) } : line
  ));
  next.words = next.words.map((word) => (
    word.line_id === lineId ? { ...word, start: Math.max(0, word.start + delta), end: Math.max(0.01, word.end + delta) } : word
  ));
  next.syllables = next.syllables.map((syllable) => (
    syllable.line_id === lineId ? { ...syllable, start: Math.max(0, syllable.start + delta), end: Math.max(0.01, syllable.end + delta) } : syllable
  ));
  return next;
}

function stretchLine(document: AlignmentDocument, lineId: string, start: number, end: number): AlignmentDocument {
  const next = cloneDocument(document);
  const line = next.lines.find((item) => item.id === lineId);
  if (!line || end <= start) return next;
  const oldStart = line.start;
  const oldDuration = Math.max(0.01, line.end - line.start);
  const scale = (end - start) / oldDuration;
  const scaleTime = (value: number): number => start + (value - oldStart) * scale;
  next.lines = next.lines.map((item) => (item.id === lineId ? { ...item, start, end } : item));
  next.words = next.words.map((word) => (
    word.line_id === lineId ? { ...word, start: scaleTime(word.start), end: scaleTime(word.end) } : word
  ));
  next.syllables = next.syllables.map((syllable) => (
    syllable.line_id === lineId ? { ...syllable, start: scaleTime(syllable.start), end: scaleTime(syllable.end) } : syllable
  ));
  return next;
}

function deleteLine(document: AlignmentDocument, lineId: string): AlignmentDocument {
  const next = cloneDocument(document);
  const wordIds = new Set(next.words.filter((word) => word.line_id === lineId).map((word) => word.id));
  next.lines = next.lines.filter((line) => line.id !== lineId);
  next.words = next.words.filter((word) => word.line_id !== lineId);
  next.syllables = next.syllables.filter((syllable) => !wordIds.has(syllable.word_id));
  next.sections = next.sections.map((section) => ({
    ...section,
    line_ids: section.line_ids.filter((id) => id !== lineId),
  }));
  return next;
}

function duplicateLine(document: AlignmentDocument, lineId: string, targetStart: number): AlignmentDocument {
  const source = document.lines.find((line) => line.id === lineId);
  if (!source) return document;
  const duration = Math.max(0.5, source.end - source.start);
  const newLineId = makeId('line');
  const rebuilt = rebuildLineFromText(
    { ...source, id: newLineId, start: targetStart, end: targetStart + duration, word_ids: [], flags: ['needs_timing_review'] },
    source.text,
  );
  const next = cloneDocument(document);
  const index = next.lines.findIndex((line) => line.id === lineId);
  next.lines.splice(index + 1, 0, rebuilt.line);
  next.words.push(...rebuilt.words);
  next.syllables.push(...rebuilt.syllables);
  next.sections = next.sections.map((section) => {
    const sectionIndex = section.line_ids.indexOf(lineId);
    if (sectionIndex === -1) return section;
    const lineIds = [...section.line_ids];
    lineIds.splice(sectionIndex + 1, 0, newLineId);
    return { ...section, line_ids: lineIds };
  });
  return next;
}

function moveLine(document: AlignmentDocument, lineId: string, direction: -1 | 1): AlignmentDocument {
  const next = cloneDocument(document);
  const index = next.lines.findIndex((line) => line.id === lineId);
  const target = index + direction;
  if (index < 0 || target < 0 || target >= next.lines.length) return next;
  const [line] = next.lines.splice(index, 1);
  next.lines.splice(target, 0, line);
  next.sections = next.sections.map((section) => {
    const lineIds = [...section.line_ids];
    const sectionIndex = lineIds.indexOf(lineId);
    const sectionTarget = sectionIndex + direction;
    if (sectionIndex < 0 || sectionTarget < 0 || sectionTarget >= lineIds.length) return section;
    const [id] = lineIds.splice(sectionIndex, 1);
    lineIds.splice(sectionTarget, 0, id);
    return { ...section, line_ids: lineIds };
  });
  return next;
}

function updateLineText(document: AlignmentDocument, lineId: string, text: string): AlignmentDocument {
  const line = document.lines.find((item) => item.id === lineId);
  if (!line) return document;
  const rebuilt = rebuildLineFromText(line, text);
  const next = cloneDocument(document);
  next.lines = next.lines.map((item) => (item.id === lineId ? rebuilt.line : item));
  next.words = next.words.filter((word) => word.line_id !== lineId).concat(rebuilt.words);
  next.syllables = next.syllables.filter((syllable) => syllable.line_id !== lineId).concat(rebuilt.syllables);
  return next;
}

function createLineAfter(document: AlignmentDocument, afterLineId: string | null, start: number): AlignmentDocument {
  const newLineId = makeId('line');
  const line: AlignmentLine = {
    id: newLineId,
    text: 'Новая строка',
    start,
    end: start + 2,
    word_ids: [],
    flags: ['needs_timing_review'],
  };
  const rebuilt = rebuildLineFromText(line, line.text);
  const next = cloneDocument(document);
  const index = afterLineId ? next.lines.findIndex((item) => item.id === afterLineId) : next.lines.length - 1;
  next.lines.splice(index + 1, 0, rebuilt.line);
  next.words.push(...rebuilt.words);
  next.syllables.push(...rebuilt.syllables);
  if (next.sections.length === 0) {
    next.sections.push({ id: makeId('section'), title: 'Main', line_ids: [newLineId] });
  } else {
    const lineIds = [...next.sections[0].line_ids];
    lineIds.splice(index + 1, 0, newLineId);
    next.sections[0] = { ...next.sections[0], line_ids: lineIds };
  }
  return next;
}

function splitLine(document: AlignmentDocument, lineId: string): AlignmentDocument {
  const line = document.lines.find((item) => item.id === lineId);
  if (!line) return document;
  const words = line.text.trim().split(/\s+/).filter(Boolean);
  if (words.length < 2) return document;
  const midpoint = Math.ceil(words.length / 2);
  const firstText = words.slice(0, midpoint).join(' ');
  const secondText = words.slice(midpoint).join(' ');
  const midTime = line.start + lineDuration(line) / 2;
  let next = updateLineText(document, lineId, firstText);
  next = createLineAfter(next, lineId, midTime);
  const inserted = next.lines[next.lines.findIndex((item) => item.id === lineId) + 1];
  next = stretchLine(next, lineId, line.start, midTime);
  next = stretchLine(next, inserted.id, midTime, line.end);
  next = updateLineText(next, inserted.id, secondText);
  return next;
}

function mergeWithNext(document: AlignmentDocument, lineId: string): AlignmentDocument {
  const index = document.lines.findIndex((line) => line.id === lineId);
  const current = document.lines[index];
  const nextLine = document.lines[index + 1];
  if (!current || !nextLine) return document;
  let next = stretchLine(document, lineId, current.start, Math.max(current.end, nextLine.end));
  next = updateLineText(next, lineId, `${current.text} ${nextLine.text}`.trim());
  next = deleteLine(next, nextLine.id);
  return next;
}

function computeLineFlags(document: AlignmentDocument, line: AlignmentLine): string[] {
  const flags = new Set(line.flags);
  const syllables = getLineSyllables(document, line);
  if (line.end <= line.start) flags.add('negative_duration');
  syllables.forEach((syllable, index) => {
    const duration = syllable.end - syllable.start;
    if (duration < 0.03) flags.add('too_short_syllable');
    if (duration > 2.5) flags.add('too_long_syllable');
    if (index > 0 && syllable.start < syllables[index - 1].end) flags.add('overlap');
  });
  if (syllables.length > 0 && syllables.length / Math.max(0.1, lineDuration(line)) > 8) {
    flags.add('line_too_dense');
  }
  return Array.from(flags);
}

export const AlignmentEditor: React.FC<AlignmentEditorProps> = ({
  payload,
  adminSecret,
  onSaveDraft,
  onPublish,
}) => {
  const [document, setDocument] = useState<AlignmentDocument>(payload.document);
  const [selectedLineId, setSelectedLineId] = useState<string | null>(payload.document.lines[0]?.id ?? null);
  const [currentTime, setCurrentTime] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [lastDraft, setLastDraft] = useState<AlignmentRevision | null>(payload.active_revision);
  const [statusText, setStatusText] = useState<string | null>(null);
  const audioRef = useRef<HTMLAudioElement>(null);

  const selectedLine = useMemo(
    () => document.lines.find((line) => line.id === selectedLineId) ?? null,
    [document.lines, selectedLineId],
  );

  const updateDocument = (updater: (current: AlignmentDocument) => AlignmentDocument): void => {
    setDocument((current) => updater(current));
    setDirty(true);
  };

  const seek = (time: number): void => {
    if (!audioRef.current) return;
    audioRef.current.currentTime = Math.max(0, time);
    setCurrentTime(audioRef.current.currentTime);
  };

  const togglePlay = async (): Promise<void> => {
    const audio = audioRef.current;
    if (!audio) return;
    if (audio.paused) {
      await audio.play();
    } else {
      audio.pause();
    }
  };

  const saveDraft = async (): Promise<void> => {
    const revision = await onSaveDraft(document);
    setLastDraft(revision);
    setDirty(false);
    setStatusText(`Черновик #${revision.revision_no} сохранён`);
  };

  const publish = async (): Promise<void> => {
    let revision = lastDraft;
    if (dirty || !revision || revision.is_published) {
      revision = await onSaveDraft(document);
      setLastDraft(revision);
      setDirty(false);
    }
    const published = await onPublish(revision.id);
    setLastDraft(published);
    setStatusText(`Версия #${published.revision_no} опубликована`);
  };

  const applySelected = (fn: (current: AlignmentDocument, lineId: string) => AlignmentDocument): void => {
    if (!selectedLineId) return;
    updateDocument((current) => fn(current, selectedLineId));
  };

  return (
    <Box sx={{ minHeight: '100vh', p: 3, color: 'white', background: '#09090f' }}>
      <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 2 }}>
        <Box>
          <Typography variant="h4" fontWeight={800}>{payload.track.artist} — {payload.track.title}</Typography>
          <Stack direction="row" spacing={1} sx={{ mt: 1 }}>
            <Chip size="small" label={`source: ${payload.track.lyrics_source ?? 'unknown'}`} />
            <Chip size="small" color={dirty ? 'warning' : 'success'} label={dirty ? 'Есть изменения' : 'Синхронизировано'} />
            {lastDraft && <Chip size="small" label={`revision #${lastDraft.revision_no}`} />}
          </Stack>
        </Box>
        <Stack direction="row" spacing={1}>
          <Button startIcon={<SaveIcon />} variant="outlined" disabled={!adminSecret} onClick={saveDraft}>Сохранить draft</Button>
          <Button startIcon={<UploadIcon />} variant="contained" disabled={!adminSecret} onClick={publish}>Опубликовать</Button>
        </Stack>
      </Stack>

      {statusText && <Alert severity="success" sx={{ mb: 2 }}>{statusText}</Alert>}
      {!adminSecret && <Alert severity="warning" sx={{ mb: 2 }}>Введите admin PIN/secret выше, чтобы сохранять и публиковать.</Alert>}

      <Paper sx={{ p: 2, mb: 2, background: 'rgba(255,255,255,0.06)', color: 'white' }}>
        {payload.stream_url ? (
          <audio
            ref={audioRef}
            src={payload.stream_url}
            onTimeUpdate={(event) => setCurrentTime(event.currentTarget.currentTime)}
            onPlay={() => setIsPlaying(true)}
            onPause={() => setIsPlaying(false)}
            style={{ width: '100%' }}
            controls
          />
        ) : (
          <Alert severity="error">У трека нет audio stream URL.</Alert>
        )}
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mt: 2 }}>
          <IconButton onClick={togglePlay} sx={{ color: 'white' }}>{isPlaying ? <PauseIcon /> : <PlayArrowIcon />}</IconButton>
          <Typography sx={{ minWidth: 80 }}>{formatTime(currentTime)}</Typography>
          {selectedLine && <Button size="small" onClick={() => seek(selectedLine.start)}>К строке</Button>}
          {selectedLine && <Button size="small" onClick={() => seek(Math.max(0, selectedLine.start - 1))}>Preview -1s</Button>}
        </Stack>
      </Paper>

      <Box sx={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 360px', gap: 2 }}>
        <Paper sx={{ p: 2, background: 'rgba(255,255,255,0.06)', color: 'white' }}>
          <Stack spacing={1}>
            {document.lines.map((line, index) => {
              const flags = computeLineFlags(document, line);
              const selected = line.id === selectedLineId;
              return (
                <Box
                  key={line.id}
                  onClick={() => setSelectedLineId(line.id)}
                  sx={{
                    p: 1.5,
                    borderRadius: 2,
                    border: selected ? '1px solid #c4b5fd' : '1px solid rgba(255,255,255,0.12)',
                    background: selected ? 'rgba(196,181,253,0.18)' : 'rgba(0,0,0,0.2)',
                    cursor: 'pointer',
                  }}
                >
                  <Stack direction="row" alignItems="center" spacing={1}>
                    <Typography sx={{ color: 'rgba(255,255,255,0.5)', width: 32 }}>{index + 1}</Typography>
                    <TextField
                      value={line.text}
                      onChange={(event) => updateDocument((current) => updateLineText(current, line.id, event.target.value))}
                      variant="standard"
                      fullWidth
                      InputProps={{ sx: { color: 'white', fontSize: 18 } }}
                    />
                    <Typography sx={{ color: 'rgba(255,255,255,0.6)', minWidth: 110 }}>
                      {formatTime(line.start)}–{formatTime(line.end)}
                    </Typography>
                  </Stack>
                  {flags.length > 0 && (
                    <Stack direction="row" spacing={0.5} sx={{ mt: 1, flexWrap: 'wrap' }}>
                      {flags.map((flag) => <Chip key={flag} size="small" color="warning" label={flag} />)}
                    </Stack>
                  )}
                </Box>
              );
            })}
          </Stack>
        </Paper>

        <Paper sx={{ p: 2, background: 'rgba(255,255,255,0.06)', color: 'white' }}>
          <Typography variant="h6" fontWeight={700}>Inspector</Typography>
          <Divider sx={{ my: 2, borderColor: 'rgba(255,255,255,0.15)' }} />
          {selectedLine ? (
            <Stack spacing={2}>
              <TextField label="Start" value={formatTime(selectedLine.start)} size="small" />
              <TextField label="End" value={formatTime(selectedLine.end)} size="small" />
              <Typography>Duration: {formatTime(lineDuration(selectedLine))} sec</Typography>
              <Stack direction="row" spacing={1}>
                <Button onClick={() => seek(selectedLine.start)}>Play from start</Button>
                <Button onClick={() => applySelected((current, id) => stretchLine(current, id, currentTime, selectedLine.end))}>Start = now</Button>
                <Button onClick={() => applySelected((current, id) => stretchLine(current, id, selectedLine.start, currentTime))}>End = now</Button>
              </Stack>
              <Stack direction="row" spacing={1}>
                <Button onClick={() => applySelected((current, id) => shiftLine(current, id, -NUDGE_LARGE))}>-250ms</Button>
                <Button onClick={() => applySelected((current, id) => shiftLine(current, id, -NUDGE_SMALL))}>-50ms</Button>
                <Button onClick={() => applySelected((current, id) => shiftLine(current, id, NUDGE_SMALL))}>+50ms</Button>
                <Button onClick={() => applySelected((current, id) => shiftLine(current, id, NUDGE_LARGE))}>+250ms</Button>
              </Stack>
              <Divider sx={{ borderColor: 'rgba(255,255,255,0.15)' }} />
              <Stack direction="row" spacing={1} flexWrap="wrap">
                <Button startIcon={<AddIcon />} onClick={() => updateDocument((current) => createLineAfter(current, selectedLine.id, currentTime))}>Insert</Button>
                <Button startIcon={<ContentCopyIcon />} onClick={() => applySelected((current, id) => duplicateLine(current, id, currentTime))}>Duplicate here</Button>
                <Button startIcon={<DeleteIcon />} color="error" onClick={() => applySelected(deleteLine)}>Delete</Button>
              </Stack>
              <Stack direction="row" spacing={1}>
                <Button startIcon={<KeyboardArrowUpIcon />} onClick={() => applySelected((current, id) => moveLine(current, id, -1))}>Up</Button>
                <Button startIcon={<KeyboardArrowDownIcon />} onClick={() => applySelected((current, id) => moveLine(current, id, 1))}>Down</Button>
              </Stack>
              <Stack direction="row" spacing={1}>
                <Button onClick={() => applySelected(splitLine)}>Split</Button>
                <Button onClick={() => applySelected(mergeWithNext)}>Merge next</Button>
              </Stack>
            </Stack>
          ) : (
            <Typography color="rgba(255,255,255,0.6)">Выберите строку.</Typography>
          )}
        </Paper>
      </Box>
    </Box>
  );
};
