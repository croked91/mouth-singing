import React, { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';
import { Rnd } from 'react-rnd';
import WaveSurfer from 'wavesurfer.js';
import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Alert,
  Box,
  Button,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  Drawer,
  IconButton,
  MenuItem,
  Paper,
  Select,
  Slider,
  Stack,
  Tab,
  Tabs,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material';
import AddIcon from '@mui/icons-material/Add';
import AlbumIcon from '@mui/icons-material/Album';
import CallMergeIcon from '@mui/icons-material/CallMerge';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import ContentCopyIcon from '@mui/icons-material/ContentCopy';
import ContentCutIcon from '@mui/icons-material/ContentCut';
import DeleteIcon from '@mui/icons-material/Delete';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import HistoryIcon from '@mui/icons-material/History';
import KeyboardArrowDownIcon from '@mui/icons-material/KeyboardArrowDown';
import KeyboardArrowUpIcon from '@mui/icons-material/KeyboardArrowUp';
import LockOpenIcon from '@mui/icons-material/LockOpen';
import MoreHorizIcon from '@mui/icons-material/MoreHoriz';
import PauseIcon from '@mui/icons-material/Pause';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';
import RedoIcon from '@mui/icons-material/Redo';
import SaveIcon from '@mui/icons-material/Save';
import SearchIcon from '@mui/icons-material/Search';
import SkipNextIcon from '@mui/icons-material/SkipNext';
import VolumeUpIcon from '@mui/icons-material/VolumeUp';
import UndoIcon from '@mui/icons-material/Undo';
import UploadIcon from '@mui/icons-material/Upload';
import ZoomInIcon from '@mui/icons-material/ZoomIn';
import ZoomOutIcon from '@mui/icons-material/ZoomOut';
import type {
  AlignmentDocument,
  AlignmentEditorPayload,
  AlignmentLine,
  AlignmentRevision,
  AlignmentSyllable,
  AlignmentWord,
  AutoRepairMode,
  AutoRepairProposal,
  AutoRepairReport,
  JobStatusEvent,
  RealignSyllablesFragmentJobResponse,
  RealignSyllablesFragmentRequest,
  RealignSyllablesFragmentResponse,
  SyllableTiming,
} from '../../types';
import { subscribeToJobStatus } from '../../services/sseService';

interface AlignmentEditorProps {
  payload: AlignmentEditorPayload;
  adminSecret: string;
  onRequestAdminSecret: () => Promise<boolean>;
  onOpenAdminSecretDialog: () => void;
  onSaveDraft: (
    document: AlignmentDocument,
    operations?: Record<string, unknown>[],
    diagnostics?: Record<string, unknown>,
  ) => Promise<AlignmentRevision>;
  onRealignLyrics: (lyricsText: string) => Promise<string>;
  onRealignSyllablesForFragment: (payload: RealignSyllablesFragmentRequest) => Promise<RealignSyllablesFragmentJobResponse>;
  onStartAutoRepair: (revisionId: string, mode: AutoRepairMode) => Promise<string>;
  onGetAutoRepairReport: (jobId: string) => Promise<AutoRepairReport>;
  onApplyAutoRepair: (jobId: string, baseRevisionId: string, proposalIds: string[]) => Promise<AlignmentRevision>;
  onReload: () => Promise<void>;
  onPublish: (revisionId: string) => Promise<AlignmentRevision>;
  onRestoreRevision: (revisionId: string) => Promise<AlignmentRevision>;
}

type Selection = { type: 'line'; id: string } | { type: 'syllable'; id: string };
type SelectedLineRange = { startLineId: string; endLineId: string };
type SelectedAudioRange = { start: number; end: number };
type EditorMode = 'review_queue' | 'full_pass';
type ReviewStatus = 'unreviewed' | 'reviewed' | 'skipped';
type IssueSeverity = 'critical' | 'high' | 'medium' | 'info';
type QueueTab = 'critical' | 'warnings' | 'unreviewed' | 'skipped' | 'done';
type WaveformDragMode = 'create' | 'move' | 'resize-start' | 'resize-end' | 'seek';
type WaveformViewport = { visibleStart: number; visibleEnd: number };

interface Issue {
  id: string;
  lineId: string;
  code: string;
  severity: IssueSeverity;
  label: string;
}

interface EditorOperation extends Record<string, unknown> {
  type: string;
  lineId?: string;
  syllableId?: string;
  at: string;
  payload?: Record<string, unknown>;
}

interface EditorSnapshot {
  document: AlignmentDocument;
  reviewState: Record<string, ReviewStatus>;
  selection: Selection | null;
}

interface FragmentRealignmentPreview {
  response: RealignSyllablesFragmentResponse;
  appliedDocument: AlignmentDocument;
  rows: {
    lineId: string;
    lineNumber: number;
    text: string;
    oldStart: number;
    oldEnd: number;
    newStart: number | null;
    newEnd: number | null;
    status: 'изменено' | 'без изменений' | 'нужно проверить';
  }[];
  warnings: string[];
}

function isFragmentRealignmentResponse(value: unknown): value is RealignSyllablesFragmentResponse {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<RealignSyllablesFragmentResponse>;
  return (
    (candidate.timing_origin === 'relative_to_fragment' || candidate.timing_origin === 'absolute_track_time')
    && typeof candidate.audio_start === 'number'
    && typeof candidate.audio_end === 'number'
    && Array.isArray(candidate.syllable_timings)
    && Array.isArray(candidate.warnings)
    && (candidate.status === 'ok' || candidate.status === 'partial' || candidate.status === 'failed')
  );
}

interface EditorState extends EditorSnapshot {
  mode: EditorMode;
  dirty: boolean;
  operations: EditorOperation[];
  undoStack: EditorSnapshot[];
  redoStack: EditorSnapshot[];
}

type EditorAction =
  | { type: 'reset'; document: AlignmentDocument; diagnostics?: Record<string, unknown> }
  | { type: 'apply'; operation: EditorOperation; updater: (document: AlignmentDocument) => AlignmentDocument; selection?: Selection | null; reviewState?: Record<string, ReviewStatus> }
  | { type: 'select'; selection: Selection | null }
  | { type: 'mode'; mode: EditorMode }
  | { type: 'markSaved' }
  | { type: 'undo' }
  | { type: 'redo' };

const NUDGE_SMALL = 0.05;
const NUDGE_LARGE = 0.25;
const MIN_BLOCK_SEC = 0.03;
const LINE_BLOCK_HEIGHT = 48;
const SYLLABLE_ROW_HEIGHT = 84;
const RECOVERY_VERSION = 1;

function makeId(prefix: string): string {
  return `${prefix}_${Math.random().toString(36).slice(2, 10)}`;
}

function nowIso(): string {
  return new Date().toISOString();
}

function formatTime(value: number): string {
  if (!Number.isFinite(value)) return '0.000';
  return value.toFixed(3);
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function normalizeAudioRange(range: SelectedAudioRange, duration: number, minDuration = 0.03): SelectedAudioRange {
  const start = clamp(Math.min(range.start, range.end), 0, duration);
  const end = clamp(Math.max(range.start, range.end), 0, duration);
  if (end - start >= minDuration) return { start, end };
  return {
    start,
    end: clamp(start + minDuration, 0, duration),
  };
}

function cloneDocument(document: AlignmentDocument): AlignmentDocument {
  return JSON.parse(JSON.stringify(document)) as AlignmentDocument;
}

function getLineDuration(line: AlignmentLine): number {
  return Math.max(0, line.end - line.start);
}

function getDocumentEnd(document: AlignmentDocument): number {
  return Math.max(0, ...document.lines.map((line) => line.end), ...document.syllables.map((s) => s.end));
}

function getLineSyllables(document: AlignmentDocument, line: AlignmentLine): AlignmentSyllable[] {
  const words = new Map(document.words.map((word) => [word.id, word]));
  const syllables = new Map(document.syllables.map((syllable) => [syllable.id, syllable]));
  return line.word_ids.flatMap((wordId) => {
    const word = words.get(wordId);
    if (!word) return [];
    return word.syllable_ids
      .map((id) => syllables.get(id))
      .filter((syllable): syllable is AlignmentSyllable => Boolean(syllable));
  });
}

function normalizeLineRange(document: AlignmentDocument, range: SelectedLineRange | null): SelectedLineRange | null {
  if (!range) return null;
  const startIndex = document.lines.findIndex((line) => line.id === range.startLineId);
  const endIndex = document.lines.findIndex((line) => line.id === range.endLineId);
  if (startIndex < 0 || endIndex < 0) return null;
  return startIndex <= endIndex
    ? range
    : { startLineId: range.endLineId, endLineId: range.startLineId };
}

function getSelectedLines(document: AlignmentDocument, range: SelectedLineRange | null): AlignmentLine[] {
  const normalized = normalizeLineRange(document, range);
  if (!normalized) return [];
  const startIndex = document.lines.findIndex((line) => line.id === normalized.startLineId);
  const endIndex = document.lines.findIndex((line) => line.id === normalized.endLineId);
  if (startIndex < 0 || endIndex < startIndex) return [];
  return document.lines.slice(startIndex, endIndex + 1);
}

function splitTextUnits(text: string): string[] {
  return text.trim().split(/\s+/).filter(Boolean);
}

function getExpectedTimingCounts(document: AlignmentDocument, lines: AlignmentLine[]): number[] {
  return lines.map((line) => {
    const existing = getLineSyllables(document, line).length;
    return Math.max(1, existing || splitTextUnits(line.text).length);
  });
}

function rebuildLineFromText(line: AlignmentLine, text: string): { line: AlignmentLine; words: AlignmentWord[]; syllables: AlignmentSyllable[] } {
  const tokens = text.trim().split(/\s+/).filter(Boolean);
  const start = line.start;
  const end = Math.max(start + 0.1, line.end);
  const duration = end - start;
  const totalChars = Math.max(1, tokens.reduce((sum, token) => sum + token.length, 0));
  let cursor = start;
  const wordIds: string[] = [];
  const words: AlignmentWord[] = [];
  const syllables: AlignmentSyllable[] = [];

  tokens.forEach((token, index) => {
    const wordId = makeId('word');
    const syllableId = makeId('syl');
    const tokenEnd = index === tokens.length - 1 ? end : cursor + duration * (token.length / totalChars);
    wordIds.push(wordId);
    words.push({ id: wordId, text: token, start: cursor, end: tokenEnd, line_id: line.id, syllable_ids: [syllableId], flags: ['needs_timing_review'] });
    syllables.push({ id: syllableId, text: token, start: cursor, end: tokenEnd, word_id: wordId, line_id: line.id, flags: ['needs_timing_review'] });
    cursor = tokenEnd;
  });

  return {
    line: { ...line, text, word_ids: wordIds, flags: Array.from(new Set([...line.flags, 'needs_timing_review'])) },
    words,
    syllables,
  };
}

function refreshParentBounds(document: AlignmentDocument, lineId?: string): AlignmentDocument {
  const next = cloneDocument(document);
  const lineIds = new Set(lineId ? [lineId] : next.lines.map((line) => line.id));

  next.words = next.words.map((word) => {
    if (!lineIds.has(word.line_id)) return word;
    const syllables = next.syllables.filter((syllable) => word.syllable_ids.includes(syllable.id));
    if (!syllables.length) return word;
    return { ...word, start: Math.min(...syllables.map((s) => s.start)), end: Math.max(...syllables.map((s) => s.end)), text: syllables.map((s) => s.text).join('') };
  });

  next.lines = next.lines.map((line) => {
    if (!lineIds.has(line.id)) return line;
    const syllables = getLineSyllables(next, line);
    if (!syllables.length) return line;
    const words = line.word_ids.map((id) => next.words.find((word) => word.id === id)).filter((word): word is AlignmentWord => Boolean(word));
    return { ...line, start: Math.min(...syllables.map((s) => s.start)), end: Math.max(...syllables.map((s) => s.end)), text: words.map((word) => word.text).join(' ').trim() || line.text };
  });

  return next;
}

function shiftLine(document: AlignmentDocument, lineId: string, delta: number): AlignmentDocument {
  const next = cloneDocument(document);
  const safeDelta = Math.max(-Math.min(...next.lines.filter((line) => line.id === lineId).map((line) => line.start), 0), delta);
  next.lines = next.lines.map((line) => (line.id === lineId ? { ...line, start: Math.max(0, line.start + safeDelta), end: Math.max(MIN_BLOCK_SEC, line.end + safeDelta) } : line));
  next.words = next.words.map((word) => (word.line_id === lineId ? { ...word, start: Math.max(0, word.start + safeDelta), end: Math.max(MIN_BLOCK_SEC, word.end + safeDelta) } : word));
  next.syllables = next.syllables.map((syllable) => (syllable.line_id === lineId ? { ...syllable, start: Math.max(0, syllable.start + safeDelta), end: Math.max(MIN_BLOCK_SEC, syllable.end + safeDelta) } : syllable));
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
  next.words = next.words.map((word) => (word.line_id === lineId ? { ...word, start: scaleTime(word.start), end: scaleTime(word.end) } : word));
  next.syllables = next.syllables.map((syllable) => (syllable.line_id === lineId ? { ...syllable, start: scaleTime(syllable.start), end: scaleTime(syllable.end) } : syllable));
  return next;
}

function updateSyllableTiming(document: AlignmentDocument, syllableId: string, start: number, end: number): AlignmentDocument {
  const source = document.syllables.find((syllable) => syllable.id === syllableId);
  if (!source || end <= start) return document;
  const next = cloneDocument(document);
  next.syllables = next.syllables.map((syllable) => (syllable.id === syllableId ? { ...syllable, start: Math.max(0, start), end: Math.max(start + MIN_BLOCK_SEC, end) } : syllable));
  return refreshParentBounds(next, source.line_id);
}

function shiftSyllable(document: AlignmentDocument, syllableId: string, delta: number): AlignmentDocument {
  const syllable = document.syllables.find((item) => item.id === syllableId);
  if (!syllable) return document;
  return updateSyllableTiming(document, syllableId, syllable.start + delta, syllable.end + delta);
}

function updateSyllableText(document: AlignmentDocument, syllableId: string, text: string): AlignmentDocument {
  const source = document.syllables.find((syllable) => syllable.id === syllableId);
  if (!source) return document;
  const next = cloneDocument(document);
  next.syllables = next.syllables.map((syllable) => (syllable.id === syllableId ? { ...syllable, text, flags: Array.from(new Set([...syllable.flags, 'needs_text_review'])) } : syllable));
  return refreshParentBounds(next, source.line_id);
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

function deleteLine(document: AlignmentDocument, lineId: string): AlignmentDocument {
  const next = cloneDocument(document);
  const wordIds = new Set(next.words.filter((word) => word.line_id === lineId).map((word) => word.id));
  next.lines = next.lines.filter((line) => line.id !== lineId);
  next.words = next.words.filter((word) => word.line_id !== lineId);
  next.syllables = next.syllables.filter((syllable) => !wordIds.has(syllable.word_id));
  next.sections = next.sections.map((section) => ({ ...section, line_ids: section.line_ids.filter((id) => id !== lineId) }));
  return next;
}

function createLineAfter(document: AlignmentDocument, afterLineId: string | null, requestedStart: number): AlignmentDocument {
  const next = cloneDocument(document);
  const index = afterLineId ? next.lines.findIndex((line) => line.id === afterLineId) : next.lines.length - 1;
  const prev = index >= 0 ? next.lines[index] : null;
  const neighbor = index >= 0 ? next.lines[index + 1] : null;
  const gapStart = prev ? prev.end : Math.max(0, requestedStart);
  const gapEnd = neighbor ? neighbor.start : gapStart + 2;
  const start = clamp(requestedStart, Math.max(0, gapStart), Math.max(gapStart, gapEnd - 0.4));
  const end = Math.max(start + 0.4, Math.min(gapEnd || start + 2, start + 2));
  const lineId = makeId('line');
  const rebuilt = rebuildLineFromText({ id: lineId, text: 'Новая строка', start, end, word_ids: [], flags: ['needs_timing_review'] }, 'Новая строка');
  next.lines.splice(index + 1, 0, rebuilt.line);
  next.words.push(...rebuilt.words);
  next.syllables.push(...rebuilt.syllables);
  if (!next.sections.length) {
    next.sections.push({ id: makeId('section'), title: 'Main', line_ids: [lineId] });
  } else {
    const sectionIndex = Math.max(0, next.sections.findIndex((section) => afterLineId && section.line_ids.includes(afterLineId)));
    const lineIds = [...next.sections[sectionIndex].line_ids];
    const insertAt = afterLineId ? lineIds.indexOf(afterLineId) + 1 : lineIds.length;
    lineIds.splice(insertAt < 0 ? lineIds.length : insertAt, 0, lineId);
    next.sections[sectionIndex] = { ...next.sections[sectionIndex], line_ids: lineIds };
  }
  return next;
}

function splitLine(document: AlignmentDocument, lineId: string, playhead?: number): AlignmentDocument {
  const line = document.lines.find((item) => item.id === lineId);
  if (!line) return document;
  const words = line.text.trim().split(/\s+/).filter(Boolean);
  if (words.length < 2) return document;
  const ratio = playhead && playhead > line.start && playhead < line.end ? (playhead - line.start) / getLineDuration(line) : 0.5;
  const splitIndex = clamp(Math.round(words.length * ratio), 1, words.length - 1);
  const midTime = playhead && playhead > line.start && playhead < line.end ? playhead : line.start + getLineDuration(line) / 2;
  let next = updateLineText(document, lineId, words.slice(0, splitIndex).join(' '));
  next = createLineAfter(next, lineId, midTime);
  const inserted = next.lines[next.lines.findIndex((item) => item.id === lineId) + 1];
  next = stretchLine(next, lineId, line.start, midTime);
  next = stretchLine(next, inserted.id, midTime, line.end);
  return updateLineText(next, inserted.id, words.slice(splitIndex).join(' '));
}

function mergeLineWithNext(document: AlignmentDocument, lineId: string): AlignmentDocument {
  const index = document.lines.findIndex((line) => line.id === lineId);
  const current = document.lines[index];
  const nextLine = document.lines[index + 1];
  if (!current || !nextLine) return document;
  let next = stretchLine(document, lineId, current.start, Math.max(current.end, nextLine.end));
  next = updateLineText(next, lineId, `${current.text} ${nextLine.text}`.trim());
  return deleteLine(next, nextLine.id);
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

function deleteSyllable(document: AlignmentDocument, syllableId: string): AlignmentDocument {
  const source = document.syllables.find((syllable) => syllable.id === syllableId);
  if (!source) return document;
  const next = cloneDocument(document);
  next.syllables = next.syllables.filter((syllable) => syllable.id !== syllableId);
  next.words = next.words.map((word) => ({ ...word, syllable_ids: word.syllable_ids.filter((id) => id !== syllableId) })).filter((word) => word.syllable_ids.length > 0 || word.line_id !== source.line_id);
  next.lines = next.lines.map((line) => ({ ...line, word_ids: line.word_ids.filter((wordId) => next.words.some((word) => word.id === wordId)) }));
  return refreshParentBounds(next, source.line_id);
}

function insertSyllable(document: AlignmentDocument, lineId: string, afterSyllableId: string | null, time: number): AlignmentDocument {
  const line = document.lines.find((item) => item.id === lineId);
  if (!line) return document;
  const next = cloneDocument(document);
  const lineSyllables = getLineSyllables(next, line);
  const after = afterSyllableId ? lineSyllables.find((syllable) => syllable.id === afterSyllableId) : null;
  const wordId = after?.word_id ?? line.word_ids[0] ?? makeId('word');
  const start = Math.max(line.start, after ? after.end : time);
  const syllable: AlignmentSyllable = { id: makeId('syl'), text: 'слог', start, end: start + 0.25, word_id: wordId, line_id: lineId, flags: ['needs_timing_review'] };
  if (!next.words.some((word) => word.id === wordId)) {
    next.words.push({ id: wordId, text: syllable.text, start: syllable.start, end: syllable.end, line_id: lineId, syllable_ids: [], flags: ['needs_timing_review'] });
    next.lines = next.lines.map((item) => (item.id === lineId ? { ...item, word_ids: [...item.word_ids, wordId] } : item));
  }
  next.syllables.push(syllable);
  next.words = next.words.map((word) => {
    if (word.id !== wordId) return word;
    const ids = [...word.syllable_ids];
    const insertAt = afterSyllableId ? ids.indexOf(afterSyllableId) + 1 : ids.length;
    ids.splice(insertAt < 0 ? ids.length : insertAt, 0, syllable.id);
    return { ...word, syllable_ids: ids };
  });
  return refreshParentBounds(next, lineId);
}

function splitSyllable(document: AlignmentDocument, syllableId: string): AlignmentDocument {
  const source = document.syllables.find((syllable) => syllable.id === syllableId);
  if (!source || source.end - source.start < 0.08) return document;
  const splitAt = Math.max(1, Math.floor(source.text.length / 2));
  const midpoint = source.start + (source.end - source.start) / 2;
  const second: AlignmentSyllable = { ...source, id: makeId('syl'), text: source.text.slice(splitAt) || source.text, start: midpoint, flags: Array.from(new Set([...source.flags, 'needs_timing_review'])) };
  const next = cloneDocument(document);
  next.syllables = next.syllables.map((syllable) => (syllable.id === syllableId ? { ...syllable, text: source.text.slice(0, splitAt) || source.text, end: midpoint, flags: Array.from(new Set([...syllable.flags, 'needs_timing_review'])) } : syllable)).concat(second);
  next.words = next.words.map((word) => {
    if (word.id !== source.word_id) return word;
    const ids = [...word.syllable_ids];
    ids.splice(ids.indexOf(syllableId) + 1, 0, second.id);
    return { ...word, syllable_ids: ids };
  });
  return refreshParentBounds(next, source.line_id);
}

function mergeSyllableWithNext(document: AlignmentDocument, syllableId: string): AlignmentDocument {
  const source = document.syllables.find((syllable) => syllable.id === syllableId);
  if (!source) return document;
  const line = document.lines.find((item) => item.id === source.line_id);
  if (!line) return document;
  const syllables = getLineSyllables(document, line);
  const nextSyllable = syllables[syllables.findIndex((syllable) => syllable.id === syllableId) + 1];
  if (!nextSyllable) return document;
  const next = cloneDocument(document);
  next.syllables = next.syllables.map((syllable) => (syllable.id === syllableId ? { ...syllable, text: `${syllable.text}${nextSyllable.text}`, end: Math.max(syllable.end, nextSyllable.end), flags: Array.from(new Set([...syllable.flags, 'needs_timing_review'])) } : syllable)).filter((syllable) => syllable.id !== nextSyllable.id);
  next.words = next.words.map((word) => ({ ...word, syllable_ids: word.syllable_ids.filter((id) => id !== nextSyllable.id) }));
  return refreshParentBounds(next, source.line_id);
}

function timingToAbsolute(timing: SyllableTiming, response: RealignSyllablesFragmentResponse): SyllableTiming {
  const offset = response.timing_origin === 'relative_to_fragment' ? response.audio_start : 0;
  return { syllable: timing.syllable, start: timing.start + offset, end: timing.end + offset };
}

function distributeTimingsByLine(
  document: AlignmentDocument,
  selectedLineIds: string[],
  response: RealignSyllablesFragmentResponse,
): Map<string, SyllableTiming[]> {
  const timings = response.syllable_timings.map((timing) => timingToAbsolute(timing, response));
  const mapping = new Map<string, SyllableTiming[]>();
  if (response.line_mapping?.length) {
    response.line_mapping.forEach((item) => {
      if (selectedLineIds.includes(item.line_id)) {
        mapping.set(item.line_id, timings.slice(item.syllable_start_index, item.syllable_end_index));
      }
    });
    return mapping;
  }

  const selectedLines = selectedLineIds
    .map((id) => document.lines.find((line) => line.id === id))
    .filter((line): line is AlignmentLine => Boolean(line));
  const counts = getExpectedTimingCounts(document, selectedLines);
  const totalExpected = counts.reduce((sum, count) => sum + count, 0);
  let cursor = 0;
  selectedLines.forEach((line, index) => {
    const remainingLines = selectedLines.length - index - 1;
    const count = index === selectedLines.length - 1
      ? timings.length - cursor
      : Math.min(timings.length - cursor - remainingLines, Math.max(1, Math.round(timings.length * counts[index] / Math.max(1, totalExpected))));
    mapping.set(line.id, timings.slice(cursor, cursor + Math.max(0, count)));
    cursor += Math.max(0, count);
  });
  return mapping;
}

function replaceLineWithTimings(
  line: AlignmentLine,
  timings: SyllableTiming[],
  needsReview: boolean,
): { line: AlignmentLine; words: AlignmentWord[]; syllables: AlignmentSyllable[] } {
  if (!timings.length) {
    return {
      line: { ...line, flags: Array.from(new Set([...line.flags, 'realigned_syllables_fragment', 'needs_review'])) },
      words: [],
      syllables: [],
    };
  }
  const wordId = makeId('word');
  const syllables = timings.map((timing) => ({
    id: makeId('syl'),
    text: timing.syllable.replace(/^\n/, ''),
    start: timing.start,
    end: timing.end,
    word_id: wordId,
    line_id: line.id,
    flags: needsReview ? ['realigned_syllables_fragment', 'needs_review'] : ['realigned_syllables_fragment'],
  }));
  const flags = Array.from(new Set([
    ...line.flags.filter((flag) => flag !== 'needs_timing_review'),
    'realigned_syllables_fragment',
    ...(needsReview ? ['needs_review'] : []),
  ]));
  return {
    line: {
      ...line,
      start: syllables[0].start,
      end: syllables[syllables.length - 1].end,
      word_ids: [wordId],
      flags,
    },
    words: [{
      id: wordId,
      text: line.text,
      start: syllables[0].start,
      end: syllables[syllables.length - 1].end,
      line_id: line.id,
      syllable_ids: syllables.map((syllable) => syllable.id),
      flags,
    }],
    syllables,
  };
}

function applySyllableRealignmentToSelectedLines(
  document: AlignmentDocument,
  selectedLineIds: string[],
  response: RealignSyllablesFragmentResponse,
): AlignmentDocument {
  const needsReview = response.status !== 'ok' || (response.confidence ?? 1) < 0.5;
  const timingMap = distributeTimingsByLine(document, selectedLineIds, response);
  const selectedSet = new Set(selectedLineIds);
  const replacements = new Map<string, ReturnType<typeof replaceLineWithTimings>>();
  document.lines.forEach((line) => {
    if (!selectedSet.has(line.id)) return;
    replacements.set(line.id, replaceLineWithTimings(line, timingMap.get(line.id) ?? [], needsReview));
  });

  return {
    sections: document.sections,
    lines: document.lines.map((line) => replacements.get(line.id)?.line ?? line),
    words: document.words.filter((word) => !selectedSet.has(word.line_id)).concat(
      Array.from(replacements.values()).flatMap((replacement) => replacement.words),
    ),
    syllables: document.syllables.filter((syllable) => !selectedSet.has(syllable.line_id)).concat(
      Array.from(replacements.values()).flatMap((replacement) => replacement.syllables),
    ),
  };
}

function buildFragmentPreview(
  document: AlignmentDocument,
  selectedLineIds: string[],
  response: RealignSyllablesFragmentResponse,
): FragmentRealignmentPreview {
  const appliedDocument = applySyllableRealignmentToSelectedLines(document, selectedLineIds, response);
  const selectedSet = new Set(selectedLineIds);
  const selectedLines = document.lines.filter((line) => selectedSet.has(line.id));
  const expected = getExpectedTimingCounts(document, selectedLines).reduce((sum, count) => sum + count, 0);
  const warnings = [...response.warnings];
  if (expected !== response.syllable_timings.length) {
    warnings.push('Количество слогов отличается от ожидаемого. Результат можно применить, но его нужно проверить.');
  }
  if (response.status === 'partial') warnings.push('Система выровняла фрагмент частично. Проверьте результат перед применением.');
  if ((response.confidence ?? 1) < 0.5) warnings.push('Низкая уверенность. Рекомендуется прослушать результат.');

  const rows: FragmentRealignmentPreview['rows'] = selectedLines.map((line) => {
    const updated = appliedDocument.lines.find((candidate) => candidate.id === line.id);
    const changed = Boolean(updated && (Math.abs(updated.start - line.start) > 0.001 || Math.abs(updated.end - line.end) > 0.001));
    const needsReview = updated?.flags.includes('needs_review') ?? true;
    return {
      lineId: line.id,
      lineNumber: document.lines.findIndex((candidate) => candidate.id === line.id) + 1,
      text: line.text,
      oldStart: line.start,
      oldEnd: line.end,
      newStart: updated?.start ?? null,
      newEnd: updated?.end ?? null,
      status: needsReview ? 'нужно проверить' : changed ? 'изменено' : 'без изменений',
    };
  });
  return { response, appliedDocument, rows, warnings };
}

function issueLabel(code: string): string {
  const labels: Record<string, string> = {
    negative_duration: 'Конец раньше начала',
    overlap: 'Перекрытие со следующим фрагментом',
    line_outside_track: 'Строка выходит за длительность трека',
    empty_timed_line: 'Пустая строка с таймингом',
    orphan_reference: 'Потеряна связь слова или слога',
    too_short_syllable: 'Слог слишком короткий',
    too_long_syllable: 'Слог слишком длинный',
    line_too_dense: 'Слишком много слогов на коротком участке',
    edge_gap: 'Слоги далеко от края строки',
    suspicious_gap: 'Подозрительная пауза между строками',
    needs_timing_review: 'Нужно проверить тайминг',
    needs_text_review: 'Нужно проверить текст',
  };
  return labels[code] ?? code;
}

function computeIssues(document: AlignmentDocument, duration: number): Issue[] {
  const issues: Issue[] = [];
  const lineIds = new Set(document.lines.map((line) => line.id));
  const wordIds = new Set(document.words.map((word) => word.id));

  document.lines.forEach((line, index) => {
    const syllables = getLineSyllables(document, line);
    const add = (code: string, severity: IssueSeverity) => issues.push({ id: `${line.id}:${code}:${issues.length}`, lineId: line.id, code, severity, label: issueLabel(code) });
    if (line.end <= line.start) add('negative_duration', 'critical');
    if (duration > 0 && line.end > duration + 0.05) add('line_outside_track', 'critical');
    if (!line.text.trim() && line.end > line.start) add('empty_timed_line', 'critical');
    if (line.word_ids.some((id) => !wordIds.has(id))) add('orphan_reference', 'critical');
    if (line.flags.includes('needs_timing_review')) add('needs_timing_review', 'info');
    if (line.flags.includes('needs_text_review')) add('needs_text_review', 'info');
    if (syllables.length > 0 && syllables.length / Math.max(0.1, getLineDuration(line)) > 8) add('line_too_dense', 'medium');
    if (syllables[0] && Math.abs(syllables[0].start - line.start) > 0.35) add('edge_gap', 'medium');
    if (syllables[syllables.length - 1] && Math.abs(line.end - syllables[syllables.length - 1].end) > 0.35) add('edge_gap', 'medium');
    syllables.forEach((syllable, syllableIndex) => {
      if (!lineIds.has(syllable.line_id) || !wordIds.has(syllable.word_id)) add('orphan_reference', 'critical');
      const syllableDuration = syllable.end - syllable.start;
      if (syllableDuration < 0.03) add('too_short_syllable', 'medium');
      if (syllableDuration > 2.5) add('too_long_syllable', 'medium');
      if (syllableIndex > 0 && syllable.start < syllables[syllableIndex - 1].end) add('overlap', 'critical');
    });
    const nextLine = document.lines[index + 1];
    if (nextLine && line.end > nextLine.start) add('overlap', 'critical');
    if (nextLine && nextLine.start - line.end > 8) add('suspicious_gap', 'medium');
  });

  return issues;
}

function activeLineAt(document: AlignmentDocument, currentTime: number): AlignmentLine | null {
  return document.lines.find((line) => currentTime >= line.start && currentTime <= line.end) ?? null;
}

function initialReviewState(document: AlignmentDocument, diagnostics?: Record<string, unknown>): Record<string, ReviewStatus> {
  const saved = diagnostics?.reviewState;
  if (saved && typeof saved === 'object' && !Array.isArray(saved)) {
    return Object.fromEntries(document.lines.map((line) => [line.id, (saved as Record<string, ReviewStatus>)[line.id] ?? 'unreviewed']));
  }
  return Object.fromEntries(document.lines.map((line) => [line.id, 'unreviewed' as ReviewStatus]));
}

function snapshot(state: EditorState): EditorSnapshot {
  return { document: state.document, reviewState: state.reviewState, selection: state.selection };
}

function editorReducer(state: EditorState, action: EditorAction): EditorState {
  if (action.type === 'reset') {
    return {
      document: action.document,
      selection: action.document.lines[0] ? { type: 'line', id: action.document.lines[0].id } : null,
      reviewState: initialReviewState(action.document, action.diagnostics),
      mode: 'review_queue',
      dirty: false,
      operations: [],
      undoStack: [],
      redoStack: [],
    };
  }
  if (action.type === 'select') return { ...state, selection: action.selection };
  if (action.type === 'mode') return { ...state, mode: action.mode };
  if (action.type === 'markSaved') return { ...state, dirty: false };
  if (action.type === 'undo') {
    const previous = state.undoStack[state.undoStack.length - 1];
    if (!previous) return state;
    return { ...state, ...previous, dirty: true, undoStack: state.undoStack.slice(0, -1), redoStack: [snapshot(state), ...state.redoStack] };
  }
  if (action.type === 'redo') {
    const next = state.redoStack[0];
    if (!next) return state;
    return { ...state, ...next, dirty: true, undoStack: [...state.undoStack, snapshot(state)], redoStack: state.redoStack.slice(1) };
  }
  if (action.type === 'apply') {
    const nextDocument = action.updater(state.document);
    return {
      ...state,
      document: nextDocument,
      selection: action.selection === undefined ? state.selection : action.selection,
      reviewState: action.reviewState ?? state.reviewState,
      dirty: true,
      operations: [...state.operations, action.operation],
      undoStack: [...state.undoStack, snapshot(state)].slice(-100),
      redoStack: [],
    };
  }
  return state;
}

function recoveryKey(payload: AlignmentEditorPayload): string {
  return `alignmentRecovery:v${RECOVERY_VERSION}:${payload.track.id}:${payload.active_revision?.id ?? 'none'}`;
}

export const AlignmentEditor: React.FC<AlignmentEditorProps> = ({
  payload,
  adminSecret,
  onRequestAdminSecret,
  onOpenAdminSecretDialog,
  onSaveDraft,
  onRealignLyrics,
  onRealignSyllablesForFragment,
  onStartAutoRepair,
  onGetAutoRepairReport,
  onApplyAutoRepair,
  onReload,
  onPublish,
  onRestoreRevision,
}) => {
  const [state, dispatch] = useReducer(editorReducer, {
    document: payload.document,
    selection: payload.document.lines[0] ? { type: 'line', id: payload.document.lines[0].id } : null,
    reviewState: initialReviewState(payload.document, payload.active_revision?.diagnostics),
    mode: 'review_queue',
    dirty: false,
    operations: [],
    undoStack: [],
    redoStack: [],
  });
  const { document, selection } = state;
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(payload.track.duration_sec ?? getDocumentEnd(payload.document));
  const [isPlaying, setIsPlaying] = useState(false);
  const [lastDraft, setLastDraft] = useState<AlignmentRevision | null>(payload.active_revision);
  const [statusText, setStatusText] = useState<string | null>(null);
  const [errorText, setErrorText] = useState<string | null>(null);
  const [queueTab, setQueueTab] = useState<QueueTab>('critical');
  const [selectedLineRange, setSelectedLineRange] = useState<SelectedLineRange | null>(
    payload.document.lines[0] ? { startLineId: payload.document.lines[0].id, endLineId: payload.document.lines[0].id } : null,
  );
  const [selectedAudioRange, setSelectedAudioRange] = useState<SelectedAudioRange | null>(null);
  const [fragmentAligning, setFragmentAligning] = useState(false);
  const [fragmentRealignProgress, setFragmentRealignProgress] = useState<string | null>(null);
  const [fragmentPreview, setFragmentPreview] = useState<FragmentRealignmentPreview | null>(null);
  const [autoRepairRunning, setAutoRepairRunning] = useState(false);
  const [autoRepairProgress, setAutoRepairProgress] = useState<string | null>(null);
  const [autoRepairReport, setAutoRepairReport] = useState<AutoRepairReport | null>(null);
  const [autoRepairApplying, setAutoRepairApplying] = useState<string | null>(null);
  const [loopingFragment, setLoopingFragment] = useState(false);
  const [waveformZoom, setWaveformZoom] = useState(128);
  const [waveformVolume, setWaveformVolume] = useState(0.85);
  const [playbackRate, setPlaybackRate] = useState(1);
  const [lineZoom, setLineZoom] = useState(90);
  const [detailZoom, setDetailZoom] = useState(420);
  const [preflightOpen, setPreflightOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [realignDialogOpen, setRealignDialogOpen] = useState(false);
  const [advancedToolsOpen, setAdvancedToolsOpen] = useState(false);
  const [replacementLyrics, setReplacementLyrics] = useState(payload.lyrics_text ?? '');
  const [realignJobId, setRealignJobId] = useState<string | null>(null);
  const [realignProgress, setRealignProgress] = useState<string | null>(null);
  const [waveformReady, setWaveformReady] = useState(false);
  const [waveformViewport, setWaveformViewport] = useState<WaveformViewport>({ visibleStart: 0, visibleEnd: payload.track.duration_sec ?? getDocumentEnd(payload.document) });
  const waveformRef = useRef<HTMLDivElement | null>(null);
  const waveSurferRef = useRef<WaveSurfer | null>(null);
  const rafRef = useRef<number | null>(null);

  const issues = useMemo(() => computeIssues(document, duration), [document, duration]);
  const criticalIssues = issues.filter((issue) => issue.severity === 'critical');
  const warningIssues = issues.filter((issue) => issue.severity === 'high' || issue.severity === 'medium');
  const selectedLine = useMemo(() => {
    if (selection?.type === 'line') return document.lines.find((line) => line.id === selection.id) ?? null;
    if (selection?.type === 'syllable') {
      const syllable = document.syllables.find((item) => item.id === selection.id);
      return syllable ? document.lines.find((line) => line.id === syllable.line_id) ?? null : null;
    }
    return document.lines[0] ?? null;
  }, [document.lines, document.syllables, selection]);
  const selectedSyllable = useMemo(() => (selection?.type === 'syllable' ? document.syllables.find((syllable) => syllable.id === selection.id) ?? null : null), [document.syllables, selection]);
  const selectedLines = useMemo(() => getSelectedLines(document, selectedLineRange), [document, selectedLineRange]);
  const selectedLineIds = useMemo(() => selectedLines.map((line) => line.id), [selectedLines]);
  const selectedTextForAlignment = useMemo(() => selectedLines.map((line) => line.text).join('\n'), [selectedLines]);
  const activeLine = useMemo(() => activeLineAt(document, currentTime), [document, currentTime]);
  const previewLine = activeLine ?? selectedLine;

  const diagnostics = useMemo<Record<string, unknown>>(() => ({
    reviewQueue: issues,
    reviewState: state.reviewState,
    preflight: { critical: criticalIssues.length, warnings: warningIssues.length, checkedAt: nowIso() },
    editorMeta: { mode: state.mode, queueTab, autosaveVersion: RECOVERY_VERSION },
  }), [criticalIssues.length, issues, queueTab, state.mode, state.reviewState, warningIssues.length]);

  useEffect(() => {
    dispatch({ type: 'reset', document: payload.document, diagnostics: payload.active_revision?.diagnostics });
    setLastDraft(payload.active_revision);
    setReplacementLyrics(payload.lyrics_text ?? '');
    setSelectedLineRange(payload.document.lines[0] ? { startLineId: payload.document.lines[0].id, endLineId: payload.document.lines[0].id } : null);
    setSelectedAudioRange(null);
    setFragmentPreview(null);
    setAutoRepairReport(null);
    setAutoRepairProgress(null);
    setAutoRepairRunning(false);
    setAutoRepairApplying(null);
    setStatusText(null);
    setErrorText(null);
  }, [payload.active_revision, payload.document, payload.lyrics_text]);

  useEffect(() => {
    const raw = window.localStorage.getItem(recoveryKey(payload));
    if (!raw) return;
    try {
      const recovery = JSON.parse(raw) as { document?: AlignmentDocument; reviewState?: Record<string, ReviewStatus>; savedAt?: string };
      if (recovery.document && window.confirm(`Найден локальный черновик от ${recovery.savedAt ?? 'недавно'}. Восстановить его?`)) {
        dispatch({ type: 'reset', document: recovery.document, diagnostics: { reviewState: recovery.reviewState ?? {} } });
        setStatusText('Локальный черновик восстановлен.');
      }
    } catch {
      window.localStorage.removeItem(recoveryKey(payload));
    }
    // Recovery is intentionally checked once per loaded revision.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [payload.track.id, payload.active_revision?.id]);

  useEffect(() => {
    if (!state.dirty) return undefined;
    const timeout = window.setTimeout(() => {
      window.localStorage.setItem(recoveryKey(payload), JSON.stringify({ document, reviewState: state.reviewState, savedAt: nowIso() }));
    }, 3500);
    return () => window.clearTimeout(timeout);
  }, [document, payload, state.dirty, state.reviewState]);

  const ensureAdminAccess = useCallback(async (silent = false): Promise<boolean> => {
    if (adminSecret) return true;
    if (silent) return false;
    setErrorText(null);
    return onRequestAdminSecret();
  }, [adminSecret, onRequestAdminSecret]);

  const saveDraft = useCallback(async (silent = false): Promise<AlignmentRevision | null> => {
    const hasAdminAccess = await ensureAdminAccess(silent);
    if (!hasAdminAccess) return null;
    try {
      const revision = await onSaveDraft(document, state.operations, diagnostics);
      setLastDraft(revision);
      dispatch({ type: 'markSaved' });
      window.localStorage.removeItem(recoveryKey(payload));
      if (!silent) setStatusText(`Черновик #${revision.revision_no} сохранён`);
      setErrorText(null);
      return revision;
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : 'Не удалось сохранить draft');
      return null;
    }
  }, [diagnostics, document, ensureAdminAccess, onSaveDraft, payload, state.operations]);

  useEffect(() => {
    if (!state.dirty || !adminSecret) return undefined;
    const timeout = window.setTimeout(() => {
      void saveDraft(true);
    }, 12000);
    return () => window.clearTimeout(timeout);
  }, [adminSecret, saveDraft, state.dirty]);

  useEffect(() => {
    if (!waveformRef.current || !payload.stream_url) return undefined;
    setWaveformReady(false);
    setWaveformViewport({ visibleStart: 0, visibleEnd: payload.track.duration_sec ?? getDocumentEnd(payload.document) });
    const wavesurfer = WaveSurfer.create({
      container: waveformRef.current,
      url: payload.stream_url,
      height: 184,
      waveColor: 'rgba(255,255,255,0.42)',
      progressColor: '#8b5cf6',
      cursorColor: '#8b5cf6',
      cursorWidth: 3,
      barWidth: 2,
      barGap: 1,
      barRadius: 2,
      normalize: true,
      minPxPerSec: waveformZoom,
      dragToSeek: true,
      autoScroll: true,
      autoCenter: true,
    });
    waveSurferRef.current = wavesurfer;
    const syncViewport = (): void => {
      const nextDuration = wavesurfer.getDuration() || payload.track.duration_sec || getDocumentEnd(payload.document);
      const width = Math.max(1, wavesurfer.getWidth());
      const scroll = Math.max(0, wavesurfer.getScroll());
      const visibleStart = scroll / Math.max(1, waveformZoom);
      const visibleEnd = Math.min(nextDuration, visibleStart + width / Math.max(1, waveformZoom));
      setWaveformViewport({ visibleStart, visibleEnd: Math.max(visibleStart + 0.001, visibleEnd) });
    };
    wavesurfer.on('ready', (audioDuration) => {
      setDuration(audioDuration || payload.track.duration_sec || getDocumentEnd(payload.document));
      wavesurfer.setVolume(waveformVolume);
      wavesurfer.setPlaybackRate(playbackRate, true);
      wavesurfer.zoom(waveformZoom);
      syncViewport();
      setWaveformReady(true);
    });
    wavesurfer.on('scroll', (visibleStartTime, visibleEndTime) => {
      setWaveformViewport({ visibleStart: visibleStartTime, visibleEnd: visibleEndTime });
    });
    wavesurfer.on('zoom', syncViewport);
    wavesurfer.on('resize', syncViewport);
    wavesurfer.on('play', () => setIsPlaying(true));
    wavesurfer.on('pause', () => setIsPlaying(false));
    wavesurfer.on('finish', () => setIsPlaying(false));
    return () => {
      setWaveformReady(false);
      wavesurfer.destroy();
      waveSurferRef.current = null;
    };
    // WaveSurfer is created once per loaded audio source; zoom/volume are applied by dedicated effects below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [payload.document, payload.stream_url, payload.track.duration_sec]);

  useEffect(() => {
    if (!waveSurferRef.current || !waveformReady) return;
    waveSurferRef.current.zoom(waveformZoom);
  }, [waveformReady, waveformZoom]);

  useEffect(() => {
    if (!waveformReady) return;
    waveSurferRef.current?.setVolume(waveformVolume);
  }, [waveformReady, waveformVolume]);

  useEffect(() => {
    if (!waveformReady) return;
    waveSurferRef.current?.setPlaybackRate(playbackRate, true);
  }, [playbackRate, waveformReady]);

  useEffect(() => {
    const tick = () => {
      if (waveSurferRef.current && isPlaying) setCurrentTime(waveSurferRef.current.getCurrentTime());
      rafRef.current = window.requestAnimationFrame(tick);
    };
    rafRef.current = window.requestAnimationFrame(tick);
    return () => {
      if (rafRef.current) window.cancelAnimationFrame(rafRef.current);
    };
  }, [isPlaying]);

  const apply = useCallback((type: string, updater: (current: AlignmentDocument) => AlignmentDocument, options?: { lineId?: string; syllableId?: string; selection?: Selection | null; reviewState?: Record<string, ReviewStatus>; payload?: Record<string, unknown> }) => {
    dispatch({ type: 'apply', operation: { type, lineId: options?.lineId, syllableId: options?.syllableId, payload: options?.payload, at: nowIso() }, updater, selection: options?.selection, reviewState: options?.reviewState });
    setStatusText(null);
    setErrorText(null);
  }, []);

  const seek = useCallback((time: number): void => {
    const nextTime = clamp(time, 0, Math.max(0.01, duration || getDocumentEnd(document)));
    waveSurferRef.current?.seekTo(nextTime / Math.max(0.01, duration || getDocumentEnd(document)));
    setCurrentTime(nextTime);
  }, [document, duration]);

  const updateAudioRange = useCallback((updater: SelectedAudioRange | null | ((current: SelectedAudioRange | null) => SelectedAudioRange | null)): void => {
    setSelectedAudioRange((current) => {
      const next = typeof updater === 'function' ? updater(current) : updater;
      if (!next) return null;
      return normalizeAudioRange(next, duration, 0.3);
    });
  }, [duration]);

  const selectLine = useCallback((line: AlignmentLine, shiftKey = false): void => {
    dispatch({ type: 'select', selection: { type: 'line', id: line.id } });
    setSelectedLineRange((current) => {
      if (!shiftKey || !current) return { startLineId: line.id, endLineId: line.id };
      return normalizeLineRange(document, { startLineId: current.startLineId, endLineId: line.id });
    });
  }, [document]);

  const togglePlay = useCallback(async (): Promise<void> => {
    await waveSurferRef.current?.playPause();
  }, []);

  const playRange = useCallback(async (start: number, end: number, leadIn = 0): Promise<void> => {
    const actualStart = Math.max(0, start - leadIn);
    seek(actualStart);
    await waveSurferRef.current?.play();
    window.setTimeout(() => waveSurferRef.current?.pause(), Math.max(80, (Math.max(start + 0.05, end) - actualStart) * 1000));
  }, [seek]);

  useEffect(() => {
    if (!loopingFragment || !selectedAudioRange || !isPlaying) return;
    if (currentTime >= selectedAudioRange.end) {
      seek(selectedAudioRange.start);
      void waveSurferRef.current?.play();
    }
  }, [currentTime, isPlaying, loopingFragment, seek, selectedAudioRange]);

  const markReviewedAndNext = useCallback(() => {
    if (!selectedLine) return;
    const reviewState = { ...state.reviewState, [selectedLine.id]: 'reviewed' as ReviewStatus };
    const currentIndex = document.lines.findIndex((line) => line.id === selectedLine.id);
    const target = document.lines.slice(currentIndex + 1).find((line) => reviewState[line.id] !== 'reviewed') ?? document.lines[currentIndex + 1] ?? selectedLine;
    apply('MARK_REVIEWED', (current) => current, { lineId: selectedLine.id, reviewState, selection: { type: 'line', id: target.id } });
    seek(target.start);
  }, [apply, document.lines, seek, selectedLine, state.reviewState]);

  const takeAudioFromSelectedLines = useCallback((padding = 1): void => {
    if (!selectedLines.length) {
      setErrorText('Выберите строки текста.');
      return;
    }
    const first = selectedLines[0];
    const last = selectedLines[selectedLines.length - 1];
    setSelectedAudioRange({
      start: clamp(first.start - padding, 0, duration),
      end: clamp(last.end + padding, 0, duration),
    });
  }, [duration, selectedLines]);

  const validateFragmentAlignment = useCallback((): string | null => {
    if (!selectedLines.length) return 'Выберите строки текста.';
    if (!selectedAudioRange) return 'Выберите участок аудио.';
    if (selectedAudioRange.end <= selectedAudioRange.start) return 'Конец отрезка должен быть позже начала.';
    const rangeDuration = selectedAudioRange.end - selectedAudioRange.start;
    if (rangeDuration < 0.3) return 'Отрезок слишком короткий.';
    if (rangeDuration > 60) return 'Для одного запуска выберите фрагмент до 60 секунд.';
    if (!selectedTextForAlignment.trim()) return 'В выбранных строках нет текста.';
    if (!payload.stream_url) return 'У трека нет аудио для выравнивания.';
    return null;
  }, [payload.stream_url, selectedAudioRange, selectedLines.length, selectedTextForAlignment]);

  const runFragmentAlignment = useCallback(async (): Promise<void> => {
    const hasAdminAccess = await ensureAdminAccess();
    if (!hasAdminAccess) return;
    const validation = validateFragmentAlignment();
    if (validation) {
      setErrorText(validation);
      return;
    }
    if (!selectedAudioRange) return;
    setFragmentAligning(true);
    setErrorText(null);
    try {
      setFragmentRealignProgress('Ставлю задачу в очередь...');
      const { job_id } = await onRealignSyllablesForFragment({
        audio_start: selectedAudioRange.start,
        audio_end: selectedAudioRange.end,
        line_ids: selectedLineIds,
        text: selectedTextForAlignment,
        preserve_line_breaks: true,
      });
      setStatusText('Выравнивание фрагмента запущено.');
      const unsubscribe = subscribeToJobStatus(job_id, (event: JobStatusEvent) => {
        if (event.status === 'completed') {
          unsubscribe();
          setFragmentAligning(false);
          setFragmentRealignProgress(null);
          if (!isFragmentRealignmentResponse(event.result)) {
            setErrorText('Сервер вернул неполный результат выравнивания.');
            return;
          }
          if (event.result.status === 'failed') {
            setErrorText('Не удалось выровнять слоги. Попробуйте расширить аудио-отрезок или проверить выбранный текст.');
            return;
          }
          setFragmentPreview(buildFragmentPreview(document, selectedLineIds, event.result));
          return;
        }
        if (event.status === 'error' || event.status === 'failed') {
          unsubscribe();
          setFragmentAligning(false);
          setFragmentRealignProgress(null);
          setErrorText(event.error ?? 'Не удалось выровнять слоги.');
          return;
        }
        setFragmentRealignProgress(`${event.step ?? 'processing'}${typeof event.progress === 'number' ? ` ${event.progress}%` : ''}`);
      }, () => {
        setFragmentAligning(false);
        setFragmentRealignProgress('Соединение со статусом прервано. Обновите редактор через несколько секунд.');
      });
    } catch (error) {
      setFragmentRealignProgress(null);
      setErrorText(error instanceof Error ? error.message : 'Не удалось выровнять слоги.');
      setFragmentAligning(false);
    }
  }, [document, ensureAdminAccess, onRealignSyllablesForFragment, selectedAudioRange, selectedLineIds, selectedTextForAlignment, validateFragmentAlignment]);

  const runAutoRepair = useCallback(async (mode: AutoRepairMode): Promise<void> => {
    const hasAdminAccess = await ensureAdminAccess();
    if (!hasAdminAccess) return;
    setAutoRepairRunning(true);
    setAutoRepairProgress('Сохраняю черновик...');
    setAutoRepairReport(null);
    setErrorText(null);
    try {
      const revision = state.dirty || !lastDraft || lastDraft.is_published
        ? await saveDraft(true)
        : lastDraft;
      if (!revision) {
        setAutoRepairRunning(false);
        setAutoRepairProgress(null);
        return;
      }
      setAutoRepairProgress('Ставлю задачу автоисправления в очередь...');
      const jobId = await onStartAutoRepair(revision.id, mode);
      const unsubscribe = subscribeToJobStatus(jobId, (event: JobStatusEvent) => {
        if (event.status === 'completed') {
          unsubscribe();
          setAutoRepairProgress('Загружаю отчёт...');
          onGetAutoRepairReport(jobId)
            .then((report) => {
              setAutoRepairReport(report);
              setAutoRepairProgress(null);
              setAutoRepairRunning(false);
              setStatusText('Автоисправление завершено. Проверьте предложения перед применением.');
            })
            .catch((error: Error) => {
              setAutoRepairProgress(null);
              setAutoRepairRunning(false);
              setErrorText(error.message);
            });
          return;
        }
        if (event.status === 'failed' || event.status === 'error') {
          unsubscribe();
          setAutoRepairProgress(null);
          setAutoRepairRunning(false);
          setErrorText(event.error ?? 'Автоисправление завершилось ошибкой.');
          return;
        }
        setAutoRepairProgress(`${event.step ?? 'processing'}${typeof event.progress === 'number' ? ` ${event.progress}%` : ''}`);
      }, () => {
        setAutoRepairProgress('Соединение со статусом автоисправления прервано. Обновите редактор через несколько секунд.');
        setAutoRepairRunning(false);
      });
    } catch (error) {
      setAutoRepairProgress(null);
      setAutoRepairRunning(false);
      setErrorText(error instanceof Error ? error.message : 'Не удалось запустить автоисправление.');
    }
  }, [ensureAdminAccess, lastDraft, onGetAutoRepairReport, onStartAutoRepair, saveDraft, state.dirty]);

  const applyAutoRepairProposals = useCallback(async (proposalIds: string[]): Promise<void> => {
    if (!autoRepairReport || proposalIds.length === 0) return;
    const hasAdminAccess = await ensureAdminAccess();
    if (!hasAdminAccess) return;
    setAutoRepairApplying(proposalIds.length === 1 ? proposalIds[0] : 'batch');
    setErrorText(null);
    try {
      const revision = await onApplyAutoRepair(
        autoRepairReport.job_id,
        autoRepairReport.base_revision_id,
        proposalIds,
      );
      if (revision.document) {
        dispatch({ type: 'reset', document: revision.document, diagnostics: revision.diagnostics });
      }
      setLastDraft(revision);
      setStatusText(
        proposalIds.length === 1
          ? 'Предложение автоисправления применено.'
          : `Применено предложений автоисправления: ${proposalIds.length}.`,
      );
      setAutoRepairReport(null);
      window.localStorage.removeItem(recoveryKey(payload));
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : 'Не удалось применить автоисправление.');
    } finally {
      setAutoRepairApplying(null);
    }
  }, [autoRepairReport, ensureAdminAccess, onApplyAutoRepair, payload]);

  const publish = async (force = false): Promise<void> => {
    const hasAdminAccess = await ensureAdminAccess();
    if (!hasAdminAccess) return;
    if (!force && (criticalIssues.length > 0 || warningIssues.length > 0 || Object.values(state.reviewState).some((status) => status !== 'reviewed'))) {
      setPreflightOpen(true);
      return;
    }
    try {
      const revision = state.dirty || !lastDraft || lastDraft.is_published ? await saveDraft(true) : lastDraft;
      if (!revision) return;
      const published = await onPublish(revision.id);
      setLastDraft(published);
      setStatusText(`Версия #${published.revision_no} опубликована`);
      setErrorText(null);
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : 'Не удалось опубликовать');
    }
  };

  const startRealign = async (): Promise<void> => {
    const hasAdminAccess = await ensureAdminAccess();
    if (!hasAdminAccess) return;
    const lyrics = replacementLyrics.trim();
    if (!lyrics) {
      setErrorText('Вставьте новый текст песни.');
      return;
    }
    try {
      setRealignProgress('Ставлю задачу в очередь...');
      const jobId = await onRealignLyrics(lyrics);
      setRealignJobId(jobId);
      setRealignDialogOpen(false);
      setStatusText('Повторное выравнивание запущено.');
      const unsubscribe = subscribeToJobStatus(jobId, (event) => {
        if (event.status === 'completed') {
          unsubscribe();
          setRealignJobId(null);
          setRealignProgress(null);
          void onReload();
          return;
        }
        if (event.status === 'error' || event.status === 'failed') {
          unsubscribe();
          setRealignJobId(null);
          setRealignProgress(null);
          setErrorText(event.error ?? 'Повторное выравнивание завершилось ошибкой.');
          return;
        }
        setRealignProgress(`${event.step ?? 'processing'}${typeof event.progress === 'number' ? ` ${event.progress}%` : ''}`);
      }, () => setRealignProgress('Соединение со статусом прервано. Обновите редактор через несколько секунд.'));
    } catch (error) {
      setRealignJobId(null);
      setRealignProgress(null);
      setErrorText(error instanceof Error ? error.message : 'Не удалось запустить повторное выравнивание');
    }
  };

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent): void => {
      const target = event.target as HTMLElement | null;
      if (target && ['INPUT', 'TEXTAREA'].includes(target.tagName)) return;
      const mod = event.ctrlKey || event.metaKey;
      if (event.code === 'Space') {
        event.preventDefault();
        void (event.shiftKey && selectedLine ? playRange(selectedLine.start, selectedLine.end, 1) : togglePlay());
      } else if (mod && event.key.toLowerCase() === 's') {
        event.preventDefault();
        void saveDraft();
      } else if (mod && event.key.toLowerCase() === 'z') {
        event.preventDefault();
        dispatch({ type: event.shiftKey ? 'redo' : 'undo' });
      } else if (mod && event.key === 'Enter') {
        event.preventDefault();
        markReviewedAndNext();
      } else if (event.key === 'ArrowDown' && selectedLine) {
        event.preventDefault();
        const next = document.lines[document.lines.findIndex((line) => line.id === selectedLine.id) + 1];
        if (next) {
          dispatch({ type: 'select', selection: { type: 'line', id: next.id } });
          seek(next.start);
        }
      } else if (event.key === 'ArrowUp' && selectedLine) {
        event.preventDefault();
        const prev = document.lines[document.lines.findIndex((line) => line.id === selectedLine.id) - 1];
        if (prev) {
          dispatch({ type: 'select', selection: { type: 'line', id: prev.id } });
          seek(prev.start);
        }
      } else if (event.altKey && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) {
        const delta = (event.key === 'ArrowLeft' ? -1 : 1) * (event.shiftKey ? NUDGE_LARGE : NUDGE_SMALL);
        event.preventDefault();
        if (selectedSyllable) apply('NUDGE_SYLLABLE', (current) => shiftSyllable(current, selectedSyllable.id, delta), { syllableId: selectedSyllable.id });
        else if (selectedLine) apply('NUDGE_LINE', (current) => shiftLine(current, selectedLine.id, delta), { lineId: selectedLine.id });
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [apply, document.lines, markReviewedAndNext, playRange, saveDraft, seek, selectedLine, selectedSyllable, togglePlay]);

  if (window.innerWidth < 1024) {
    return (
      <Box sx={{ minHeight: '100vh', p: 3, color: 'white', background: '#09090f' }}>
        <Alert severity="warning">Полный timing editor доступен на экранах от 1024px. Откройте редактор на desktop или tablet landscape.</Alert>
      </Box>
    );
  }

  const selectedLineIssues = selectedLine ? issues.filter((issue) => issue.lineId === selectedLine.id) : [];
  const reviewedCount = document.lines.filter((line) => state.reviewState[line.id] === 'reviewed').length;
  const skippedCount = document.lines.filter((line) => state.reviewState[line.id] === 'skipped').length;
  const unreviewedCount = Math.max(0, document.lines.length - reviewedCount - skippedCount);
  const progressPercent = document.lines.length ? Math.round((reviewedCount / document.lines.length) * 100) : 0;

  return (
    <Box sx={{ minHeight: '100vh', p: { xs: 2, xl: 3 }, color: 'white', background: 'radial-gradient(circle at top, rgba(79,70,229,0.16), transparent 28%), #09090f' }}>
      <Stack spacing={2}>
        <Paper sx={{ p: 2, borderRadius: 3, background: 'rgba(12,16,28,0.92)', border: '1px solid rgba(255,255,255,0.08)', backdropFilter: 'blur(16px)' }}>
          <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ gap: 2, flexWrap: 'wrap' }}>
            <Stack direction="row" spacing={2} alignItems="center" sx={{ minWidth: 0 }}>
              <Box sx={{ width: 42, height: 42, borderRadius: 2, display: 'grid', placeItems: 'center', background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.1)' }}>
                <AlbumIcon />
              </Box>
              <Box sx={{ minWidth: 0 }}>
                <Typography variant="h5" fontWeight={800} sx={{ overflowWrap: 'anywhere' }}>
                  {payload.track.artist} — {payload.track.title}
                </Typography>
              </Box>
              <Chip color={criticalIssues.length ? 'warning' : 'default'} label={`${criticalIssues.length + warningIssues.length} проблем`} />
              <Chip color="success" label={`${reviewedCount} из ${document.lines.length} проверено`} />
              <Chip
                color={payload.stream_source === 'vocals' ? 'success' : 'warning'}
                variant="outlined"
                label={payload.stream_source === 'vocals' ? 'audio: vocal stem' : 'audio: instrumental'}
              />
            </Stack>
            <Stack direction="row" spacing={1} alignItems="center" sx={{ flexWrap: 'wrap' }}>
              <Chip
                icon={<LockOpenIcon />}
                label={adminSecret ? 'Админ-доступ активен' : 'Админ-доступ'}
                color={adminSecret ? 'success' : 'default'}
                onClick={onOpenAdminSecretDialog}
                variant={adminSecret ? 'filled' : 'outlined'}
              />
              <Tooltip title="История версий">
                <IconButton onClick={() => setHistoryOpen(true)} sx={{ color: 'white' }}>
                  <HistoryIcon />
                </IconButton>
              </Tooltip>
              <Tooltip title="Undo">
                <span><IconButton disabled={!state.undoStack.length} onClick={() => dispatch({ type: 'undo' })} sx={{ color: 'white' }}><UndoIcon /></IconButton></span>
              </Tooltip>
              <Tooltip title="Redo">
                <span><IconButton disabled={!state.redoStack.length} onClick={() => dispatch({ type: 'redo' })} sx={{ color: 'white' }}><RedoIcon /></IconButton></span>
              </Tooltip>
              <Button startIcon={<SaveIcon />} variant="outlined" onClick={() => void saveDraft()} sx={{ minWidth: 150 }}>
                Сохранить
              </Button>
              <Button startIcon={<UploadIcon />} variant="contained" onClick={() => void publish(false)} sx={{ minWidth: 168 }}>
                Опубликовать
              </Button>
              <Tooltip title="Дополнительно">
                <IconButton onClick={() => setAdvancedToolsOpen((value) => !value)} sx={{ color: 'white' }}>
                  <MoreHorizIcon />
                </IconButton>
              </Tooltip>
            </Stack>
          </Stack>
        </Paper>

        <Box role="status" aria-live="polite">
          {statusText && <Alert severity="success" sx={{ mb: 2 }}>{statusText}</Alert>}
          {realignProgress && <Alert severity="info" sx={{ mb: 2 }}>Повторное выравнивание: {realignProgress}</Alert>}
          {fragmentRealignProgress && <Alert severity="info" sx={{ mb: 2 }}>Выравнивание фрагмента: {fragmentRealignProgress}</Alert>}
          {autoRepairProgress && <Alert severity="info" sx={{ mb: 2 }}>Автоисправление: {autoRepairProgress}</Alert>}
        </Box>
        {errorText && <Alert severity="error" sx={{ mb: 2 }}>{errorText}</Alert>}

        <WaveformHeroPanel
          waveformRef={waveformRef}
          hasStream={Boolean(payload.stream_url)}
          audioRange={selectedAudioRange}
          duration={duration}
          currentTime={currentTime}
          isPlaying={isPlaying}
          waveformZoom={waveformZoom}
          viewport={waveformViewport}
          waveformVolume={waveformVolume}
          playbackRate={playbackRate}
          onVolumeChange={setWaveformVolume}
          onPlaybackRateChange={setPlaybackRate}
          onZoomChange={setWaveformZoom}
          onZoomIn={() => setWaveformZoom((value) => clamp(value + 24, 72, 320))}
          onZoomOut={() => setWaveformZoom((value) => clamp(value - 24, 72, 320))}
          onTogglePlay={() => void togglePlay()}
          onPlayFragment={() => selectedAudioRange && void playRange(selectedAudioRange.start, selectedAudioRange.end)}
          onSeek={seek}
          onAudioRangeChange={updateAudioRange}
        />

        <CurrentLineFocusPanel
          line={previewLine}
          previousLine={document.lines[previewLine ? document.lines.findIndex((item) => item.id === previewLine.id) - 1 : -1] ?? null}
          nextLine={document.lines[previewLine ? document.lines.findIndex((item) => item.id === previewLine.id) + 1 : -1] ?? null}
          issues={selectedLineIssues}
          syllableTimings={previewLine ? getLineSyllables(document, previewLine).map((syllable) => ({ syllable: syllable.text, start: syllable.start, end: syllable.end })) : []}
          currentTime={currentTime}
          isPlaying={isPlaying}
          onSetStartNow={() => selectedLine && apply('SET_LINE_START_TO_PLAYHEAD', (current) => stretchLine(current, selectedLine.id, currentTime, selectedLine.end), { lineId: selectedLine.id })}
          onSetEndNow={() => selectedLine && apply('SET_LINE_END_TO_PLAYHEAD', (current) => stretchLine(current, selectedLine.id, selectedLine.start, currentTime), { lineId: selectedLine.id })}
          onNudgeBack={() => selectedLine && apply('NUDGE_LINE', (current) => shiftLine(current, selectedLine.id, -NUDGE_SMALL), { lineId: selectedLine.id })}
          onNudgeForward={() => selectedLine && apply('NUDGE_LINE', (current) => shiftLine(current, selectedLine.id, NUDGE_SMALL), { lineId: selectedLine.id })}
          onMarkReviewed={markReviewedAndNext}
        />

        <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', xl: 'minmax(0, 1.15fr) minmax(360px, 0.95fr) minmax(320px, 0.9fr)' }, gap: 2 }}>
          <LineListVirtualized
            document={document}
            issues={issues}
            reviewState={state.reviewState}
            selectedLineId={selectedLine?.id ?? null}
            selectedLineIds={new Set(selectedLineIds)}
            activeLineId={activeLine?.id ?? null}
            onAddLine={() => {
              const anchorLine = selectedLine ?? document.lines[document.lines.length - 1] ?? null;
              const requestedStart = anchorLine ? anchorLine.end : currentTime;
              apply('CREATE_LINE_AFTER', (current) => createLineAfter(current, anchorLine?.id ?? null, requestedStart), {
                lineId: anchorLine?.id,
              });
            }}
            onDeleteLine={(line) => {
              if (!window.confirm(`Удалить строку "${line.text || `#${document.lines.findIndex((item) => item.id === line.id) + 1}`}"?`)) return;
              const currentIndex = document.lines.findIndex((item) => item.id === line.id);
              const fallback = document.lines[currentIndex + 1] ?? document.lines[currentIndex - 1] ?? null;
              apply('DELETE_LINE', (current) => deleteLine(current, line.id), {
                lineId: line.id,
                selection: fallback ? { type: 'line', id: fallback.id } : null,
              });
            }}
            onSelect={(line, shiftKey) => {
              selectLine(line, shiftKey);
              seek(line.start);
            }}
            onPlay={(line) => void playRange(line.start, line.end, 1)}
            onMarkReviewed={(line) => {
              const nextStatus: ReviewStatus = state.reviewState[line.id] === 'reviewed' ? 'unreviewed' : 'reviewed';
              apply('MARK_REVIEWED', (current) => current, {
                lineId: line.id,
                reviewState: { ...state.reviewState, [line.id]: nextStatus },
                payload: { reviewStatus: nextStatus },
              });
            }}
          />

          <Stack spacing={2}>
            <AutoRepairPanel
              report={autoRepairReport}
              running={autoRepairRunning}
              applying={autoRepairApplying}
              onRun={() => void runAutoRepair('propose')}
              onRunSafe={() => void runAutoRepair('auto_apply_safe')}
              onClear={() => setAutoRepairReport(null)}
              onApply={(proposalIds) => void applyAutoRepairProposals(proposalIds)}
            />
            <FragmentRealignmentPanel
              selectedLines={selectedLines}
              document={document}
              audioRange={selectedAudioRange}
              duration={duration}
              currentTime={currentTime}
              aligning={fragmentAligning}
              onTakeFromLines={() => takeAudioFromSelectedLines(1)}
              onSetStartNow={() => updateAudioRange((range) => ({ start: currentTime, end: Math.max(currentTime + 0.3, range?.end ?? currentTime + 5) }))}
              onSetEndNow={() => updateAudioRange((range) => ({ start: Math.min(range?.start ?? Math.max(0, currentTime - 5), currentTime - 0.3), end: currentTime }))}
              onExpand={() => selectedAudioRange && updateAudioRange({ start: clamp(selectedAudioRange.start - 1, 0, duration), end: clamp(selectedAudioRange.end + 1, 0, duration) })}
              onShrinkToLines={() => takeAudioFromSelectedLines(0)}
              onReset={() => {
                updateAudioRange(null);
                setLoopingFragment(false);
              }}
              onPlay={() => selectedAudioRange && void playRange(selectedAudioRange.start, selectedAudioRange.end)}
              onLoop={() => {
                if (!selectedAudioRange) return;
                setLoopingFragment((value) => !value);
                seek(selectedAudioRange.start);
                void waveSurferRef.current?.play();
              }}
              looping={loopingFragment}
              onRun={() => void runFragmentAlignment()}
            />
          </Stack>

          <Stack spacing={2}>
            <InspectorPanel
              selectedLine={selectedLine}
              selectedSyllable={selectedSyllable}
              selectedLineIssues={selectedLineIssues}
              currentTime={currentTime}
              onPlayRange={playRange}
              onSelectLine={() => selectedLine && dispatch({ type: 'select', selection: { type: 'line', id: selectedLine.id } })}
              onApply={apply}
              onMarkReviewed={markReviewedAndNext}
            />
            <ReviewSummaryPanel
              issueCount={criticalIssues.length + warningIssues.length}
              unreviewedCount={unreviewedCount}
              skippedCount={skippedCount}
              reviewedCount={reviewedCount}
              progressPercent={progressPercent}
              onShowAll={() => setQueueTab('warnings')}
            />
          </Stack>
        </Box>

        <Accordion expanded={advancedToolsOpen} onChange={(_, expanded) => setAdvancedToolsOpen(expanded)} sx={{ background: 'rgba(12,16,28,0.92)', color: 'white', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '20px !important', overflow: 'hidden' }}>
          <AccordionSummary expandIcon={<ExpandMoreIcon sx={{ color: 'white' }} />}>
            <Typography fontWeight={700}>Дополнительная детализация</Typography>
          </AccordionSummary>
          <AccordionDetails>
            <Stack spacing={2}>
              <Stack direction={{ xs: 'column', md: 'row' }} spacing={1.5} alignItems={{ md: 'center' }} justifyContent="space-between">
                <Stack direction="row" spacing={1.5} alignItems="center">
                  <Typography variant="body2" color="rgba(255,255,255,0.6)">Режим</Typography>
                  <Select size="small" value={state.mode} onChange={(event) => dispatch({ type: 'mode', mode: event.target.value as EditorMode })} sx={{ minWidth: 170, color: 'white' }}>
                    <MenuItem value="review_queue">Review Queue</MenuItem>
                    <MenuItem value="full_pass">Полный проход</MenuItem>
                  </Select>
                  <Typography variant="body2" color="rgba(255,255,255,0.6)">Timeline</Typography>
                  <Slider min={45} max={180} step={5} value={lineZoom} onChange={(_, value) => setLineZoom(value as number)} sx={{ width: 140 }} />
                </Stack>
                <Button variant="outlined" disabled={Boolean(realignJobId)} onClick={() => setRealignDialogOpen(true)}>
                  Новый текст
                </Button>
              </Stack>
              <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', xl: state.mode === 'review_queue' ? '300px minmax(0, 1fr)' : 'minmax(0, 1fr)' }, gap: 2 }}>
                {state.mode === 'review_queue' && (
                  <ProblemQueuePanel
                    issues={issues}
                    document={document}
                    reviewState={state.reviewState}
                    queueTab={queueTab}
                    onTab={setQueueTab}
                    onSelect={(lineId) => {
                      dispatch({ type: 'select', selection: { type: 'line', id: lineId } });
                      const line = document.lines.find((item) => item.id === lineId);
                      if (line) seek(line.start);
                    }}
                  />
                )}
                <Stack spacing={2}>
                  <LineTimeline
                    document={document}
                    selectedLine={selectedLine}
                    selectedLineIds={new Set(selectedLineIds)}
                    activeLine={activeLine}
                    currentTime={currentTime}
                    lineZoom={lineZoom}
                    duration={duration}
                    issues={issues}
                    onSelect={(line) => selectLine(line)}
                    onSeek={seek}
                    onMove={(line, nextStart) => apply('SHIFT_LINE', (current) => shiftLine(current, line.id, nextStart - line.start), { lineId: line.id, selection: { type: 'line', id: line.id } })}
                    onResize={(line, start, end) => apply('STRETCH_LINE', (current) => stretchLine(current, line.id, start, end), { lineId: line.id, selection: { type: 'line', id: line.id } })}
                  />
                  <LineDetailEditor
                    document={document}
                    selectedLine={selectedLine}
                    selectedSyllable={selectedSyllable}
                    currentTime={currentTime}
                    detailZoom={detailZoom}
                    onZoom={setDetailZoom}
                    onSelectSyllable={(syllable) => dispatch({ type: 'select', selection: { type: 'syllable', id: syllable.id } })}
                    onSeek={seek}
                    onMoveSyllable={(syllable, nextStart) => apply('SHIFT_SYLLABLE', (current) => shiftSyllable(current, syllable.id, nextStart - syllable.start), { syllableId: syllable.id, selection: { type: 'syllable', id: syllable.id } })}
                    onResizeSyllable={(syllable, start, end) => apply('STRETCH_SYLLABLE', (current) => updateSyllableTiming(current, syllable.id, start, end), { syllableId: syllable.id, selection: { type: 'syllable', id: syllable.id } })}
                  />
                </Stack>
              </Box>
            </Stack>
          </AccordionDetails>
        </Accordion>
      </Stack>

      <PublishPreflightDialog
        open={preflightOpen}
        criticalIssues={criticalIssues}
        warningIssues={warningIssues}
        unresolvedCount={Object.values(state.reviewState).filter((status) => status !== 'reviewed').length}
        onClose={() => setPreflightOpen(false)}
        onPublishAnyway={() => {
          setPreflightOpen(false);
          void publish(true);
        }}
      />

      <RevisionHistoryDrawer
        open={historyOpen}
        revisions={payload.revisions}
        activeRevisionId={payload.active_revision?.id ?? null}
        onClose={() => setHistoryOpen(false)}
        onRequestAdminSecret={onRequestAdminSecret}
        onRestore={async (revisionId) => {
          const revision = await onRestoreRevision(revisionId);
          setLastDraft(revision);
          setHistoryOpen(false);
          await onReload();
        }}
      />

      <FragmentRealignmentPreviewDialog
        preview={fragmentPreview}
        onClose={() => setFragmentPreview(null)}
        onPlay={() => fragmentPreview && void playRange(fragmentPreview.response.audio_start, fragmentPreview.response.audio_end)}
        onApply={() => {
          if (!fragmentPreview) return;
          apply('APPLY_SYLLABLE_REALIGNMENT_FRAGMENT', () => fragmentPreview.appliedDocument, {
            payload: {
              lineIds: selectedLineIds,
              audioStart: fragmentPreview.response.audio_start,
              audioEnd: fragmentPreview.response.audio_end,
              status: fragmentPreview.response.status,
              confidence: fragmentPreview.response.confidence,
            },
          });
          setFragmentPreview(null);
          setStatusText('Слоги выровнены для выбранного фрагмента');
        }}
      />

      <Dialog open={realignDialogOpen} onClose={() => setRealignDialogOpen(false)} fullWidth maxWidth="md">
        <DialogTitle>Заменить текст и выровнять заново</DialogTitle>
        <DialogContent>
          <Alert severity="warning" sx={{ mb: 2 }}>Будет создана новая черновая версия выравнивания. Опубликованный трек не изменится до публикации.</Alert>
          <TextField label="Новый текст песни" value={replacementLyrics} onChange={(event) => setReplacementLyrics(event.target.value)} multiline minRows={12} fullWidth autoFocus />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setRealignDialogOpen(false)}>Отмена</Button>
          <Button variant="contained" disabled={Boolean(realignJobId)} onClick={() => void startRealign()}>Отправить на выравнивание</Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
};

function WaveformHeroPanel({
  waveformRef,
  hasStream,
  audioRange,
  duration,
  currentTime,
  isPlaying,
  waveformZoom,
  viewport,
  waveformVolume,
  playbackRate,
  onVolumeChange,
  onPlaybackRateChange,
  onZoomChange,
  onZoomIn,
  onZoomOut,
  onTogglePlay,
  onPlayFragment,
  onSeek,
  onAudioRangeChange,
}: {
  waveformRef: React.RefObject<HTMLDivElement | null>;
  hasStream: boolean;
  audioRange: SelectedAudioRange | null;
  duration: number;
  currentTime: number;
  isPlaying: boolean;
  waveformZoom: number;
  viewport: WaveformViewport;
  waveformVolume: number;
  playbackRate: number;
  onVolumeChange: (value: number) => void;
  onPlaybackRateChange: (value: number) => void;
  onZoomChange: (value: number) => void;
  onZoomIn: () => void;
  onZoomOut: () => void;
  onTogglePlay: () => void;
  onPlayFragment: () => void;
  onSeek: (time: number) => void;
  onAudioRangeChange: (updater: SelectedAudioRange | null | ((current: SelectedAudioRange | null) => SelectedAudioRange | null)) => void;
}) {
  const interactionRef = useRef<HTMLDivElement | null>(null);
  const PLAYHEAD_KNOB_HEIGHT = 22;
  const SCROLLBAR_CLEARANCE = 18;
  const dragStateRef = useRef<{
    mode: WaveformDragMode;
    pointerId: number;
    anchorTime: number;
    initialRange: SelectedAudioRange | null;
  } | null>(null);
  const visibleDuration = Math.max(0.001, viewport.visibleEnd - viewport.visibleStart);
  const rateOptions = [0.5, 0.75, 1, 1.25, 1.5];
  const majorTick = visibleDuration > 90 ? 15 : visibleDuration > 45 ? 10 : visibleDuration > 20 ? 5 : visibleDuration > 10 ? 2 : 1;
  const ticks = useMemo(() => {
    const items: number[] = [];
    const firstTick = Math.floor(viewport.visibleStart / majorTick) * majorTick;
    for (let time = firstTick; time <= viewport.visibleEnd + majorTick; time += majorTick) {
      if (time >= viewport.visibleStart - 0.001 && time <= viewport.visibleEnd + 0.001) items.push(time);
    }
    return items;
  }, [majorTick, viewport.visibleEnd, viewport.visibleStart]);
  const getTimeFromClientX = useCallback((clientX: number): number => {
    const rect = interactionRef.current?.getBoundingClientRect();
    if (!rect || rect.width <= 0 || duration <= 0) return 0;
    const ratio = clamp((clientX - rect.left) / rect.width, 0, 1);
    return clamp(viewport.visibleStart + ratio * visibleDuration, 0, duration);
  }, [duration, viewport.visibleStart, visibleDuration]);
  const beginDrag = useCallback((event: React.PointerEvent<HTMLDivElement>, mode: WaveformDragMode): void => {
    if (!hasStream || duration <= 0) return;
    const anchorTime = getTimeFromClientX(event.clientX);
    dragStateRef.current = {
      mode,
      pointerId: event.pointerId,
      anchorTime,
      initialRange: audioRange,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
    if (mode === 'seek') {
      onSeek(anchorTime);
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    if (mode === 'create') onAudioRangeChange({ start: anchorTime, end: anchorTime + 0.3 });
    event.preventDefault();
    event.stopPropagation();
  }, [audioRange, duration, getTimeFromClientX, hasStream, onAudioRangeChange, onSeek]);
  const handlePointerMove = useCallback((event: React.PointerEvent<HTMLDivElement>): void => {
    const drag = dragStateRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const nextTime = getTimeFromClientX(event.clientX);
    if (drag.mode === 'seek') {
      onSeek(nextTime);
      return;
    }
    if (drag.mode === 'create') {
      onAudioRangeChange({ start: drag.anchorTime, end: nextTime });
      return;
    }
    if (!drag.initialRange) return;
    if (drag.mode === 'resize-start') {
      onAudioRangeChange({ start: nextTime, end: drag.initialRange.end });
      return;
    }
    if (drag.mode === 'resize-end') {
      onAudioRangeChange({ start: drag.initialRange.start, end: nextTime });
      return;
    }
    const delta = nextTime - drag.anchorTime;
    let start = drag.initialRange.start + delta;
    let end = drag.initialRange.end + delta;
    if (start < 0) {
      end -= start;
      start = 0;
    }
    if (end > duration) {
      start -= end - duration;
      end = duration;
    }
    onAudioRangeChange({ start, end: Math.max(start + 0.3, end) });
  }, [duration, getTimeFromClientX, onAudioRangeChange, onSeek]);
  const endDrag = useCallback((event: React.PointerEvent<HTMLDivElement>): void => {
    const drag = dragStateRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    dragStateRef.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  }, []);

  return (
    <Paper sx={{ p: 2, borderRadius: 3, background: 'linear-gradient(180deg, rgba(17,24,39,0.96), rgba(12,16,28,0.92))', border: '1px solid rgba(255,255,255,0.08)', overflow: 'hidden' }}>
      <Stack direction={{ xs: 'column', xl: 'row' }} spacing={2} alignItems={{ xl: 'center' }}>
        <Stack spacing={2} sx={{ width: { xl: 210 }, flexShrink: 0 }}>
          <Stack direction="row" spacing={2} alignItems="center">
            <IconButton onClick={onTogglePlay} sx={{ width: 64, height: 64, background: 'linear-gradient(135deg, #7c3aed, #4f46e5)', color: 'white', '&:hover': { background: 'linear-gradient(135deg, #8b5cf6, #6366f1)' } }}>
              {isPlaying ? <PauseIcon sx={{ fontSize: 30 }} /> : <PlayArrowIcon sx={{ fontSize: 30 }} />}
            </IconButton>
            <Typography variant="h5" fontWeight={700} sx={{ fontVariantNumeric: 'tabular-nums' }}>
              {formatTime(currentTime)} <Typography component="span" sx={{ color: 'rgba(255,255,255,0.45)', fontSize: '1.2rem' }}>/ {formatTime(duration)}</Typography>
            </Typography>
          </Stack>
          <Stack direction="row" spacing={1.5} alignItems="center">
            <VolumeUpIcon sx={{ color: 'rgba(255,255,255,0.72)' }} />
            <Slider min={0} max={1} step={0.05} value={waveformVolume} onChange={(_, value) => onVolumeChange(value as number)} />
          </Stack>
        </Stack>

        <Box sx={{ flex: 1, minWidth: 0 }}>
          {hasStream ? (
            <Box sx={{ borderRadius: 3, border: '1px solid rgba(255,255,255,0.06)', background: 'rgba(9,12,22,0.78)', p: 1.5 }}>
              <Box sx={{ position: 'relative', height: 28, mb: 1 }}>
                {ticks.map((time) => (
                  <Box key={time} sx={{ position: 'absolute', left: `${((time - viewport.visibleStart) / visibleDuration) * 100}%`, top: 0, transform: 'translateX(-50%)', color: 'rgba(255,255,255,0.62)' }}>
                    <Typography variant="caption">{formatTime(time)}</Typography>
                    <Box sx={{ width: 1, height: 10, mt: 0.5, background: 'rgba(255,255,255,0.18)', mx: 'auto' }} />
                  </Box>
                ))}
              </Box>
              <Box sx={{ position: 'relative', minHeight: 184 }} onDoubleClick={(event) => {
                const rect = event.currentTarget.getBoundingClientRect();
                const ratio = clamp((event.clientX - rect.left) / rect.width, 0, 1);
                onSeek(viewport.visibleStart + ratio * visibleDuration);
              }}>
                <Box ref={waveformRef} sx={{ minHeight: 184 }} />
                <Box
                  ref={interactionRef}
                  onPointerDown={(event) => {
                    const target = event.target as HTMLElement | null;
                    if (target?.dataset.role === 'playhead') return beginDrag(event, 'seek');
                    if (target?.dataset.handle === 'start') return beginDrag(event, 'resize-start');
                    if (target?.dataset.handle === 'end') return beginDrag(event, 'resize-end');
                    if (target?.dataset.role === 'selection') return beginDrag(event, 'move');
                    if (target?.dataset.role === 'ruler') return beginDrag(event, 'seek');
                    beginDrag(event, 'create');
                  }}
                  onPointerMove={handlePointerMove}
                  onPointerUp={endDrag}
                  onPointerCancel={endDrag}
                  sx={{ position: 'absolute', left: 0, right: 0, top: 0, bottom: SCROLLBAR_CLEARANCE, zIndex: 6, cursor: 'crosshair' }}
                >
                  <Box
                    data-role="ruler"
                    sx={{ position: 'absolute', left: 0, right: 0, top: 0, height: PLAYHEAD_KNOB_HEIGHT, cursor: 'pointer' }}
                  />
                  <Box
                    data-role="playhead"
                    sx={{
                      position: 'absolute',
                      top: 0,
                      bottom: 0,
                      left: `${((currentTime - viewport.visibleStart) / visibleDuration) * 100}%`,
                      width: 2,
                      background: 'rgba(139,92,246,0.95)',
                      boxShadow: '0 0 0 1px rgba(139,92,246,0.18)',
                      transform: 'translateX(-1px)',
                      pointerEvents: currentTime >= viewport.visibleStart && currentTime <= viewport.visibleEnd ? 'auto' : 'none',
                      opacity: currentTime >= viewport.visibleStart && currentTime <= viewport.visibleEnd ? 1 : 0,
                      cursor: 'ew-resize',
                    }}
                  >
                    <Box
                      data-role="playhead"
                      sx={{
                        position: 'absolute',
                        top: 8,
                        left: '50%',
                        transform: 'translate(-50%, -100%)',
                        width: 12,
                        height: 12,
                        borderRadius: 999,
                        background: '#a78bfa',
                        boxShadow: '0 0 0 2px rgba(10,14,24,0.9), 0 0 14px rgba(167,139,250,0.45)',
                      }}
                    />
                  </Box>
                  {audioRange && duration > 0 && (
                    <Box
                      data-role="selection"
                      sx={{
                        position: 'absolute',
                        top: 0,
                        bottom: 0,
                        left: `${((Math.max(audioRange.start, viewport.visibleStart) - viewport.visibleStart) / visibleDuration) * 100}%`,
                        width: `${(Math.max(0, Math.min(audioRange.end, viewport.visibleEnd) - Math.max(audioRange.start, viewport.visibleStart)) / visibleDuration) * 100}%`,
                        background: 'linear-gradient(180deg, rgba(139,92,246,0.45), rgba(99,102,241,0.22))',
                        border: '1px solid rgba(255,255,255,0.3)',
                        borderRadius: 1.5,
                        cursor: 'grab',
                        display: audioRange.end > viewport.visibleStart && audioRange.start < viewport.visibleEnd ? 'block' : 'none',
                      }}
                    >
                      <Box
                        data-handle="start"
                        sx={{
                          position: 'absolute',
                          left: -6,
                          top: 8,
                          bottom: 8,
                          width: 12,
                          borderRadius: 999,
                          background: 'rgba(255,255,255,0.95)',
                          boxShadow: '0 0 0 1px rgba(15,23,42,0.35)',
                          cursor: 'ew-resize',
                        }}
                      />
                      <Box
                        data-handle="end"
                        sx={{
                          position: 'absolute',
                          right: -6,
                          top: 8,
                          bottom: 8,
                          width: 12,
                          borderRadius: 999,
                          background: 'rgba(255,255,255,0.95)',
                          boxShadow: '0 0 0 1px rgba(15,23,42,0.35)',
                          cursor: 'ew-resize',
                        }}
                      />
                    </Box>
                  )}
                </Box>
                {audioRange && duration > 0 && (
                  <Box
                    sx={{
                      position: 'absolute',
                      top: 0,
                      bottom: 0,
                      left: `${((Math.max(audioRange.start, viewport.visibleStart) - viewport.visibleStart) / visibleDuration) * 100}%`,
                      width: `${(Math.max(0, Math.min(audioRange.end, viewport.visibleEnd) - Math.max(audioRange.start, viewport.visibleStart)) / visibleDuration) * 100}%`,
                      background: 'linear-gradient(180deg, rgba(139,92,246,0.45), rgba(99,102,241,0.22))',
                      borderLeft: '3px solid rgba(255,255,255,0.95)',
                      borderRight: '3px solid rgba(255,255,255,0.95)',
                      borderRadius: 1.5,
                      pointerEvents: 'none',
                      zIndex: 5,
                      display: audioRange.end > viewport.visibleStart && audioRange.start < viewport.visibleEnd ? 'block' : 'none',
                    }}
                  />
                )}
              </Box>
            </Box>
          ) : (
            <Alert severity="error">У трека нет audio stream URL.</Alert>
          )}
        </Box>

        <Stack spacing={1.5} sx={{ width: { xl: 220 }, flexShrink: 0 }}>
          <Button variant="outlined" size="large" onClick={onPlayFragment} disabled={!audioRange}>
            Слушать фрагмент
          </Button>
        </Stack>
      </Stack>

      <Stack direction={{ xs: 'column', xl: 'row' }} alignItems={{ xl: 'center' }} justifyContent="space-between" spacing={2} sx={{ mt: 2 }}>
        <Stack spacing={1.25}>
          <Stack direction="row" spacing={1.5} alignItems="center" sx={{ flexWrap: 'wrap' }}>
            <Typography fontWeight={700}>Масштаб</Typography>
            <Typography variant="caption" color="rgba(255,255,255,0.5)">сжать</Typography>
            <IconButton size="small" onClick={onZoomOut} sx={{ color: 'white', border: '1px solid rgba(255,255,255,0.12)' }}>
              <ZoomOutIcon fontSize="small" />
            </IconButton>
            <Slider min={72} max={320} step={8} value={waveformZoom} onChange={(_, value) => onZoomChange(value as number)} sx={{ width: 220 }} />
            <IconButton size="small" onClick={onZoomIn} sx={{ color: 'white', border: '1px solid rgba(255,255,255,0.12)' }}>
              <ZoomInIcon fontSize="small" />
            </IconButton>
            <Typography variant="caption" color="rgba(255,255,255,0.5)">приблизить</Typography>
            <Typography sx={{ fontVariantNumeric: 'tabular-nums', color: 'rgba(255,255,255,0.72)' }}>{waveformZoom} px/s</Typography>
          </Stack>
          <Stack direction="row" spacing={1} alignItems="center" sx={{ flexWrap: 'wrap' }}>
            <Typography fontWeight={700}>Скорость</Typography>
            {rateOptions.map((rate) => (
              <Button
                key={rate}
                size="small"
                variant={playbackRate === rate ? 'contained' : 'outlined'}
                onClick={() => onPlaybackRateChange(rate)}
                sx={{ minWidth: 64, fontVariantNumeric: 'tabular-nums' }}
              >
                {rate}x
              </Button>
            ))}
          </Stack>
        </Stack>
        <Stack direction="row" spacing={1} alignItems="center">
          <Button variant="outlined" onClick={() => onSeek(Math.max(0, currentTime - 15))}>{'|<'}</Button>
          <Button variant="outlined" onClick={() => onSeek(Math.max(0, currentTime - 5))}>-5 c</Button>
          <IconButton onClick={onTogglePlay} sx={{ width: 52, height: 52, background: 'linear-gradient(135deg, #7c3aed, #4f46e5)', color: 'white' }}>
            {isPlaying ? <PauseIcon /> : <PlayArrowIcon />}
          </IconButton>
          <Button variant="outlined" onClick={() => onSeek(Math.min(duration, currentTime + 5))}>+5 c</Button>
          <Button variant="outlined" onClick={() => onSeek(Math.min(duration, currentTime + 15))}>{'>|'}</Button>
        </Stack>
      </Stack>
    </Paper>
  );
}

function CurrentLineFocusPanel({
  line,
  previousLine,
  nextLine,
  issues,
  syllableTimings,
  currentTime,
  isPlaying,
  onSetStartNow,
  onSetEndNow,
  onNudgeBack,
  onNudgeForward,
  onMarkReviewed,
}: {
  line: AlignmentLine | null;
  previousLine: AlignmentLine | null;
  nextLine: AlignmentLine | null;
  issues: Issue[];
  syllableTimings: SyllableTiming[];
  currentTime: number;
  isPlaying: boolean;
  onSetStartNow: () => void;
  onSetEndNow: () => void;
  onNudgeBack: () => void;
  onNudgeForward: () => void;
  onMarkReviewed: () => void;
}) {
  return (
    <Paper sx={{ p: 2.5, borderRadius: 3, background: 'rgba(12,16,28,0.92)', border: '1px solid rgba(255,255,255,0.08)' }}>
      <Stack spacing={2}>
        <Chip label="Текущая строка" size="small" color="secondary" sx={{ alignSelf: 'flex-start' }} />
        <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', lg: 'minmax(0, 1fr) minmax(0, 1.4fr) minmax(0, 1fr)' }, gap: 2, alignItems: 'center' }}>
          <Box sx={{ minWidth: 0 }}>
            <Typography variant="caption" color="rgba(255,255,255,0.5)">Предыдущая строка</Typography>
            <Typography color="rgba(255,255,255,0.78)" sx={{ mt: 0.5 }}>{previousLine?.text ?? ' '}</Typography>
          </Box>
          <Box sx={{ textAlign: 'center', minWidth: 0 }}>
            {issues[0] && <Chip label={issues[0].label} color="warning" sx={{ mb: 1 }} />}
            {line ? (
              <Box>
                {syllableTimings.length > 0 && (
                  <Box sx={{ mt: 0.5 }}>
                    <Typography variant="caption" color="rgba(255,255,255,0.52)" sx={{ display: 'block', mb: 0.75 }}>
                      Предпросмотр подсветки
                    </Typography>
                    <CompactSyllablePreview syllableTimings={syllableTimings} currentTime={currentTime} isPlaying={isPlaying} />
                  </Box>
                )}
                {syllableTimings.length === 0 && (
                  <Typography sx={{ fontSize: { xs: 24, xl: 30 }, lineHeight: 1.15, fontWeight: 700, overflowWrap: 'anywhere', textWrap: 'balance', color: 'rgba(255,255,255,0.88)' }}>
                    {line.text}
                  </Typography>
                )}
              </Box>
            ) : (
              <Typography sx={{ fontSize: { xs: 32, xl: 46 }, lineHeight: 1.05, fontWeight: 800, overflowWrap: 'anywhere' }}>
                Выберите строку из списка слева
              </Typography>
            )}
          </Box>
          <Box sx={{ minWidth: 0, textAlign: { lg: 'right' } }}>
            <Typography variant="caption" color="rgba(255,255,255,0.5)">Следующая строка</Typography>
            <Typography color="rgba(255,255,255,0.78)" sx={{ mt: 0.5 }}>{nextLine?.text ?? ' '}</Typography>
          </Box>
        </Box>
        <Stack direction={{ xs: 'column', lg: 'row' }} spacing={1.5}>
          <Button variant="outlined" onClick={onSetStartNow} disabled={!line}>Начало = сейчас</Button>
          <Button variant="outlined" onClick={onSetEndNow} disabled={!line}>Конец = сейчас</Button>
          <Button variant="outlined" onClick={onNudgeBack} disabled={!line}>Сдвинуть -50 мс</Button>
          <Button variant="outlined" onClick={onNudgeForward} disabled={!line}>Сдвинуть +50 мс</Button>
          <Button variant="contained" onClick={onMarkReviewed} disabled={!line} sx={{ ml: { lg: 'auto' }, minWidth: 240 }}>
            Проверено и дальше
          </Button>
        </Stack>
      </Stack>
    </Paper>
  );
}

function CompactSyllablePreview({
  syllableTimings,
  currentTime,
  isPlaying,
}: {
  syllableTimings: SyllableTiming[];
  currentTime: number;
  isPlaying: boolean;
}) {
  const progress = useMemo(() => {
    if (!syllableTimings.length) return 0;
    const first = syllableTimings[0].start;
    const last = syllableTimings[syllableTimings.length - 1].end;
    const duration = Math.max(0.001, last - first);
    return clamp((currentTime - first) / duration, 0, 1);
  }, [currentTime, syllableTimings]);

  return (
    <Box
      sx={{
        width: '100%',
        maxWidth: 760,
        mx: 'auto',
        px: 1.5,
        py: 1.25,
        borderRadius: 2,
        background: 'rgba(255,255,255,0.03)',
        border: '1px solid rgba(255,255,255,0.08)',
      }}
    >
      <Box
        sx={{
          display: 'flex',
          flexWrap: 'wrap',
          justifyContent: 'center',
          columnGap: 0.35,
          rowGap: 0.2,
          fontSize: { xs: 22, xl: 26 },
          lineHeight: 1.2,
          fontWeight: 600,
        }}
      >
        {syllableTimings.map((syllable, index) => {
          const isDone = currentTime >= syllable.end;
          const isActive = currentTime >= syllable.start && currentTime < syllable.end;
          const activeProgress = isActive
            ? clamp((currentTime - syllable.start) / Math.max(0.001, syllable.end - syllable.start), 0, 1) * 100
            : 0;

          return (
            <Box
              key={`${syllable.syllable}-${index}-${syllable.start}`}
              component="span"
              sx={{
                color: isDone ? 'rgba(255,255,255,0.96)' : isActive ? 'transparent' : 'rgba(255,255,255,0.42)',
                background: isActive ? `linear-gradient(90deg, #f5d0fe ${activeProgress}%, rgba(255,255,255,0.42) ${activeProgress}%)` : 'none',
                backgroundClip: isActive ? 'text' : undefined,
                WebkitBackgroundClip: isActive ? 'text' : undefined,
                WebkitTextFillColor: isActive ? 'transparent' : undefined,
                textShadow: isDone ? '0 0 16px rgba(255,255,255,0.12)' : isActive ? '0 0 18px rgba(167,139,250,0.18)' : 'none',
                transition: isPlaying ? 'none' : 'color 120ms ease, text-shadow 120ms ease',
              }}
            >
              {syllable.syllable}
            </Box>
          );
        })}
      </Box>
      <Box sx={{ mt: 1, height: 3, borderRadius: 999, background: 'rgba(255,255,255,0.09)', overflow: 'hidden' }}>
        <Box
          sx={{
            width: `${progress * 100}%`,
            height: '100%',
            background: 'linear-gradient(90deg, #7c3aed, #67e8f9)',
            boxShadow: '0 0 14px rgba(103,232,249,0.18)',
            transition: isPlaying ? 'none' : 'width 120ms ease',
          }}
        />
      </Box>
    </Box>
  );
}

function AutoRepairPanel({ report, running, applying, onRun, onRunSafe, onClear, onApply }: {
  report: AutoRepairReport | null;
  running: boolean;
  applying: string | null;
  onRun: () => void;
  onRunSafe: () => void;
  onClear: () => void;
  onApply: (proposalIds: string[]) => void;
}) {
  const actionable = report?.proposals.filter((proposal) => proposal.decision !== 'blocked') ?? [];
  const confident = actionable.filter((proposal) => proposal.decision === 'auto_apply');
  return (
    <Paper sx={{ p: 2, borderRadius: 3, background: 'rgba(12,16,28,0.92)', color: 'white', border: '1px solid rgba(255,255,255,0.08)' }}>
      <Stack spacing={2}>
        <Box>
          <Typography variant="h6" fontWeight={800}>Автоисправление проблемных участков</Typography>
          <Typography variant="body2" color="rgba(255,255,255,0.68)" sx={{ mt: 0.5 }}>
            Система сама найдёт проблемные строки, переберёт аудио-диапазоны и предложит новые слоговые тайминги.
          </Typography>
        </Box>

        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1.25}>
          <Button fullWidth variant="contained" disabled={running} onClick={onRun}>
            {running ? 'Ищу исправления...' : 'Найти исправления'}
          </Button>
          <Button fullWidth variant="outlined" disabled={running} onClick={onRunSafe}>
            Автоисправить уверенные
          </Button>
        </Stack>

        {!report && (
          <Typography variant="body2" color="rgba(255,255,255,0.48)">
            Это не заменяет ручную проверку: результат появится как список кандидатов, которые можно применить выборочно.
          </Typography>
        )}

        {report && (
          <Stack spacing={1.25}>
            <Stack direction="row" spacing={1} flexWrap="wrap">
              <Chip size="small" label={`Строк: ${report.summary.clusters}`} />
              <Chip size="small" color="success" label={`Уверенных: ${report.summary.auto_apply}`} />
              <Chip size="small" color="warning" label={`Проверить: ${report.summary.needs_review}`} />
              <Chip size="small" label={`Отклонено: ${report.summary.rejected}`} />
              <Chip size="small" color="error" label={`Blocked: ${report.summary.blocked}`} />
            </Stack>

            {report.warnings.length > 0 && (
              <Alert severity="warning">{report.warnings.join(' ')}</Alert>
            )}

            {confident.length > 1 && (
              <Button
                variant="outlined"
                disabled={Boolean(applying)}
                onClick={() => onApply(confident.map((proposal) => proposal.id))}
              >
                {applying === 'batch' ? 'Применяю...' : `Применить уверенные (${confident.length})`}
              </Button>
            )}

            <Stack spacing={1} sx={{ maxHeight: 360, overflowY: 'auto', pr: 0.5 }}>
              {report.proposals.length === 0 && (
                <Typography variant="body2" color="rgba(255,255,255,0.55)">
                  Кандидатов не найдено.
                </Typography>
              )}
              {report.proposals.map((proposal) => (
                <AutoRepairProposalRow
                  key={proposal.id}
                  proposal={proposal}
                  applying={applying === proposal.id}
                  onApply={() => onApply([proposal.id])}
                />
              ))}
            </Stack>

            <Button size="small" onClick={onClear}>Скрыть отчёт</Button>
          </Stack>
        )}
      </Stack>
    </Paper>
  );
}

function AutoRepairProposalRow({ proposal, applying, onApply }: {
  proposal: AutoRepairProposal;
  applying: boolean;
  onApply: () => void;
}) {
  const decisionColor: 'success' | 'warning' | 'error' | 'default' = proposal.decision === 'auto_apply'
    ? 'success'
    : proposal.decision === 'needs_review'
      ? 'warning'
      : proposal.decision === 'blocked'
        ? 'error'
        : 'default';
  return (
    <Box sx={{ p: 1.25, borderRadius: 2, background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.08)' }}>
      <Stack spacing={0.75}>
        <Stack direction="row" justifyContent="space-between" gap={1} alignItems="flex-start">
          <Box sx={{ minWidth: 0 }}>
            <Stack direction="row" spacing={0.75} flexWrap="wrap" alignItems="center">
              <Chip size="small" color={decisionColor} label={proposal.decision} />
              <Chip size="small" label={`${Math.round(proposal.score * 100)}%`} />
              {proposal.locator_method && (
                <Chip size="small" label={proposal.locator_method} />
              )}
              {proposal.locator_confidence != null && (
                <Chip size="small" label={`locator ${Math.round(proposal.locator_confidence * 100)}%`} />
              )}
              <Typography variant="caption" color="rgba(255,255,255,0.55)">
                строк: {proposal.line_ids.length}
              </Typography>
            </Stack>
            <Typography sx={{ mt: 0.75, fontSize: 13, color: 'rgba(255,255,255,0.86)', overflowWrap: 'anywhere' }}>
              {proposal.text.split('\n').slice(0, 2).join(' / ')}
            </Typography>
            {proposal.matched_text && (
              <Typography variant="caption" color="rgba(255,255,255,0.62)" sx={{ display: 'block', mt: 0.35, overflowWrap: 'anywhere' }}>
                ASR: {proposal.matched_text}
              </Typography>
            )}
            <Typography variant="caption" color="rgba(255,255,255,0.5)" sx={{ display: 'block', mt: 0.5, fontVariantNumeric: 'tabular-nums' }}>
              {formatTime(proposal.old_audio_range.start)}–{formatTime(proposal.old_audio_range.end)}
              {' → '}
              {formatTime(proposal.new_audio_range.start)}–{formatTime(proposal.new_audio_range.end)}
            </Typography>
            {(proposal.phoneme_score != null || proposal.text_score != null) && (
              <Typography variant="caption" color="rgba(255,255,255,0.44)" sx={{ display: 'block', mt: 0.35 }}>
                phoneme {Math.round((proposal.phoneme_score ?? 0) * 100)}%
                {' · '}
                text {Math.round((proposal.text_score ?? 0) * 100)}%
              </Typography>
            )}
          </Box>
          <Button
            size="small"
            variant="outlined"
            disabled={proposal.decision === 'blocked' || applying}
            onClick={onApply}
            sx={{ whiteSpace: 'nowrap' }}
          >
            {applying ? 'Применяю...' : 'Применить'}
          </Button>
        </Stack>
        {(proposal.warnings.length > 0 || proposal.reasons.length > 0) && (
          <Typography variant="caption" color="rgba(255,255,255,0.52)">
            {[...proposal.reasons, ...proposal.warnings].slice(0, 3).join(' · ')}
          </Typography>
        )}
      </Stack>
    </Box>
  );
}

function FragmentRealignmentPanel({ selectedLines, document, audioRange, duration, currentTime, aligning, looping, onTakeFromLines, onSetStartNow, onSetEndNow, onExpand, onShrinkToLines, onReset, onPlay, onLoop, onRun }: {
  selectedLines: AlignmentLine[];
  document: AlignmentDocument;
  audioRange: SelectedAudioRange | null;
  duration: number;
  currentTime: number;
  aligning: boolean;
  looping: boolean;
  onTakeFromLines: () => void;
  onSetStartNow: () => void;
  onSetEndNow: () => void;
  onExpand: () => void;
  onShrinkToLines: () => void;
  onReset: () => void;
  onPlay: () => void;
  onLoop: () => void;
  onRun: () => void;
}) {
  const firstIndex = selectedLines[0] ? document.lines.findIndex((line) => line.id === selectedLines[0].id) + 1 : 0;
  const lastIndex = selectedLines[selectedLines.length - 1] ? document.lines.findIndex((line) => line.id === selectedLines[selectedLines.length - 1].id) + 1 : 0;
  const canRun = Boolean(audioRange) && selectedLines.length > 0 && !aligning;
  return (
    <Paper sx={{ p: 2, borderRadius: 3, background: 'rgba(12,16,28,0.92)', color: 'white', border: '1px solid rgba(255,255,255,0.08)' }}>
      <Stack spacing={2}>
        <Box>
          <Typography variant="h6" fontWeight={800}>Выровнять слоги по аудио</Typography>
          <Typography variant="body2" color="rgba(255,255,255,0.68)" sx={{ mt: 0.5 }}>
            Слушайте фрагмент и корректируйте время, чтобы слова совпадали с вокалом.
          </Typography>
        </Box>
        <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 1.25 }}>
          <Box sx={{ p: 1.5, borderRadius: 2, background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.08)' }}>
            <Typography variant="caption" color="rgba(255,255,255,0.55)">Выбрано строк</Typography>
            <Typography sx={{ mt: 0.5, fontSize: 28, fontWeight: 800 }}>{selectedLines.length || 0}</Typography>
            <Typography variant="body2" color="rgba(255,255,255,0.62)">
              {selectedLines.length ? `строки #${firstIndex}-${lastIndex}` : 'Выберите строки'}
            </Typography>
          </Box>
          <Box sx={{ p: 1.5, borderRadius: 2, background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.08)' }}>
            <Typography variant="caption" color="rgba(255,255,255,0.55)">Выбранный диапазон</Typography>
            <Typography sx={{ mt: 0.5, fontSize: 24, fontWeight: 800, fontVariantNumeric: 'tabular-nums' }}>
              {audioRange ? `${formatTime(audioRange.start)} — ${formatTime(audioRange.end)}` : 'Не выбран'}
            </Typography>
            <Typography variant="body2" color="rgba(255,255,255,0.62)">
              {audioRange ? `${formatTime(audioRange.end - audioRange.start)} сек` : `${formatTime(currentTime)} / ${formatTime(duration)}`}
            </Typography>
          </Box>
        </Box>
        <Button fullWidth variant="contained" size="large" sx={{ py: 1.5 }} disabled={!canRun} onClick={onRun}>
          {aligning ? 'Выравниваю...' : 'Выровнять слоги в выбранном фрагменте'}
        </Button>
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1.25}>
          <Button fullWidth variant="outlined" onClick={onTakeFromLines}>Взять звук по строкам</Button>
          <Button fullWidth variant="outlined" onClick={onReset}>Сбросить</Button>
        </Stack>
        <Stack direction="row" spacing={1} flexWrap="wrap">
          <Button size="small" onClick={onSetStartNow}>Начало = сейчас</Button>
          <Button size="small" onClick={onSetEndNow}>Конец = сейчас</Button>
          <Button size="small" disabled={!audioRange} onClick={onPlay}>Послушать отрезок</Button>
          <Button size="small" disabled={!audioRange} onClick={onLoop}>{looping ? 'Остановить цикл' : 'Зациклить отрезок'}</Button>
          <Button size="small" disabled={!audioRange} onClick={onExpand}>Расширить на 1 сек</Button>
          <Button size="small" disabled={!selectedLines.length} onClick={onShrinkToLines}>Сжать к строкам</Button>
        </Stack>
        <Typography variant="body2" color="rgba(255,255,255,0.48)">
          {!audioRange || !selectedLines.length
            ? 'Выберите строки и задайте диапазон на waveform.'
            : 'Слушайте фрагмент и при необходимости уточняйте границы перед выравниванием.'}
        </Typography>
      </Stack>
    </Paper>
  );
}

function FragmentRealignmentPreviewDialog({ preview, onClose, onPlay, onApply }: {
  preview: FragmentRealignmentPreview | null;
  onClose: () => void;
  onPlay: () => void;
  onApply: () => void;
}) {
  return (
    <Dialog open={Boolean(preview)} onClose={onClose} fullWidth maxWidth="md">
      <DialogTitle>Проверить новые слоговые тайминги</DialogTitle>
      <DialogContent>
        {preview && (
          <Stack spacing={2}>
            <Stack direction="row" spacing={1} flexWrap="wrap">
              <Chip label={`${formatTime(preview.response.audio_start)}-${formatTime(preview.response.audio_end)}`} />
              <Chip label={`${preview.response.syllable_timings.length} слогов`} />
              <Chip label={`origin: ${preview.response.timing_origin}`} />
              <Chip label={`status: ${preview.response.status}`} color={preview.response.status === 'ok' ? 'success' : 'warning'} />
              {typeof preview.response.confidence === 'number' && <Chip label={`confidence: ${Math.round(preview.response.confidence * 100)}%`} />}
            </Stack>
            {preview.warnings.length > 0 && <Alert severity="warning">{preview.warnings.join(' ')}</Alert>}
            <Stack spacing={1}>
              {preview.rows.map((row) => (
                <Paper key={row.lineId} sx={{ p: 1.5, background: 'rgba(255,255,255,0.05)' }}>
                  <Stack direction="row" spacing={1} alignItems="center" justifyContent="space-between">
                    <Typography fontWeight={700}>#{row.lineNumber} {row.text}</Typography>
                    <Chip size="small" label={row.status} color={row.status === 'нужно проверить' ? 'warning' : row.status === 'изменено' ? 'success' : 'default'} />
                  </Stack>
                  <Typography variant="body2" color="rgba(255,255,255,0.72)" sx={{ mt: 0.5 }}>
                    Было {formatTime(row.oldStart)}-{formatTime(row.oldEnd)} · стало {row.newStart == null || row.newEnd == null ? 'нет данных' : `${formatTime(row.newStart)}-${formatTime(row.newEnd)}`}
                  </Typography>
                  {row.newStart != null && row.newEnd != null && (
                    <Typography variant="caption" color="rgba(255,255,255,0.58)">
                      delta start {formatTime(row.newStart - row.oldStart)} · delta end {formatTime(row.newEnd - row.oldEnd)}
                    </Typography>
                  )}
                </Paper>
              ))}
            </Stack>
          </Stack>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onPlay} disabled={!preview}>Послушать результат</Button>
        <Button onClick={onClose}>Отмена</Button>
        <Button variant="contained" onClick={onApply} disabled={!preview}>Применить</Button>
      </DialogActions>
    </Dialog>
  );
}

function ProblemQueuePanel({ issues, document, reviewState, queueTab, onTab, onSelect }: {
  issues: Issue[];
  document: AlignmentDocument;
  reviewState: Record<string, ReviewStatus>;
  queueTab: QueueTab;
  onTab: (tab: QueueTab) => void;
  onSelect: (lineId: string) => void;
}) {
  const items = useMemo(() => {
    if (queueTab === 'critical') return issues.filter((issue) => issue.severity === 'critical');
    if (queueTab === 'warnings') return issues.filter((issue) => issue.severity === 'high' || issue.severity === 'medium');
    if (queueTab === 'unreviewed') return document.lines.filter((line) => reviewState[line.id] === 'unreviewed').map((line) => ({ id: line.id, lineId: line.id, label: line.text || 'Пустая строка', code: 'unreviewed', severity: 'info' as IssueSeverity }));
    if (queueTab === 'skipped') return document.lines.filter((line) => reviewState[line.id] === 'skipped').map((line) => ({ id: line.id, lineId: line.id, label: line.text || 'Пустая строка', code: 'skipped', severity: 'info' as IssueSeverity }));
    return document.lines.filter((line) => reviewState[line.id] === 'reviewed').map((line) => ({ id: line.id, lineId: line.id, label: line.text || 'Пустая строка', code: 'done', severity: 'info' as IssueSeverity }));
  }, [document.lines, issues, queueTab, reviewState]);
  return (
    <Paper sx={{ p: 1.5, background: 'rgba(255,255,255,0.06)', color: 'white', alignSelf: 'start', position: 'sticky', top: 92 }}>
      <Typography variant="h6" fontWeight={700}>Очередь проверки</Typography>
      <Tabs value={queueTab} onChange={(_, value) => onTab(value)} variant="scrollable" sx={{ minHeight: 36, mb: 1 }}>
        <Tab value="critical" label="Критично" />
        <Tab value="warnings" label="Warnings" />
        <Tab value="unreviewed" label="Не проверено" />
        <Tab value="skipped" label="Skip" />
        <Tab value="done" label="Готово" />
      </Tabs>
      <Stack spacing={1} sx={{ maxHeight: 620, overflowY: 'auto' }}>
        {items.length === 0 ? <Alert severity="success">Нет элементов.</Alert> : items.map((item) => {
          const line = document.lines.find((candidate) => candidate.id === item.lineId);
          return (
            <Button key={item.id} onClick={() => onSelect(item.lineId)} sx={{ justifyContent: 'flex-start', textAlign: 'left', color: 'white', border: '1px solid rgba(255,255,255,0.12)' }}>
              <Box sx={{ minWidth: 0 }}>
                <Typography variant="body2" fontWeight={700} noWrap>{item.label}</Typography>
                <Typography variant="caption" color="rgba(255,255,255,0.58)" noWrap>{line?.text ?? ''}</Typography>
              </Box>
            </Button>
          );
        })}
      </Stack>
    </Paper>
  );
}

function LineListVirtualized({ document, issues, reviewState, selectedLineId, selectedLineIds, activeLineId, onAddLine, onDeleteLine, onSelect, onPlay, onMarkReviewed }: {
  document: AlignmentDocument;
  issues: Issue[];
  reviewState: Record<string, ReviewStatus>;
  selectedLineId: string | null;
  selectedLineIds: Set<string>;
  activeLineId: string | null;
  onAddLine: () => void;
  onDeleteLine: (line: AlignmentLine) => void;
  onSelect: (line: AlignmentLine, shiftKey: boolean) => void;
  onPlay: (line: AlignmentLine) => void;
  onMarkReviewed: (line: AlignmentLine) => void;
}) {
  const issuesByLine = useMemo(() => {
    const map = new Map<string, Issue[]>();
    issues.forEach((issue) => map.set(issue.lineId, [...(map.get(issue.lineId) ?? []), issue]));
    return map;
  }, [issues]);
  return (
    <Paper sx={{ p: 2, borderRadius: 3, background: 'rgba(12,16,28,0.92)', color: 'white', border: '1px solid rgba(255,255,255,0.08)' }}>
      <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 1.5 }}>
        <Typography variant="h6" fontWeight={700}>Строки песни</Typography>
        <Stack direction="row" spacing={0.5}>
          <Tooltip title="Добавить строку">
            <IconButton size="small" onClick={onAddLine} sx={{ color: 'rgba(255,255,255,0.88)' }}>
              <AddIcon fontSize="small" />
            </IconButton>
          </Tooltip>
          <IconButton size="small" sx={{ color: 'rgba(255,255,255,0.75)' }}>
            <SearchIcon fontSize="small" />
          </IconButton>
        </Stack>
      </Stack>
      <Box sx={{ height: 420, overflow: 'auto', border: '1px solid rgba(255,255,255,0.12)', borderRadius: 1, p: 0.5 }}>
        <Stack spacing={0.7}>
          {document.lines.map((line, index) => {
            const selected = selectedLineId === line.id;
            const inRange = selectedLineIds.has(line.id);
            const active = activeLineId === line.id;
            const reviewed = reviewState[line.id] === 'reviewed';
            const lineIssues = issuesByLine.get(line.id) ?? [];
            const hasProblem = lineIssues.some((issue) => issue.severity === 'critical' || issue.severity === 'high' || issue.severity === 'medium');
            const statusLabel = reviewed ? 'Проверено' : hasProblem ? 'Проблема' : 'Не проверено';
            const statusColor = reviewed ? 'success' : hasProblem ? 'warning' : 'default';
            return (
              <Box
                key={line.id}
                role="button"
                tabIndex={0}
                onClick={(event) => onSelect(line, event.shiftKey)}
                onKeyDown={(event) => event.key === 'Enter' && onSelect(line, event.shiftKey)}
                sx={{
                  display: 'grid',
                  gridTemplateColumns: '42px minmax(0, 1fr) auto',
                  gap: 1,
                  alignItems: 'center',
                  px: 1.25,
                  py: 0.95,
                  borderRadius: 1.5,
                  border: selected
                    ? '1px solid rgba(139,92,246,0.95)'
                    : inRange
                      ? '1px solid rgba(139,92,246,0.55)'
                      : active
                        ? '1px solid rgba(125,211,252,0.55)'
                        : reviewed
                          ? '1px solid rgba(34,197,94,0.28)'
                          : '1px solid rgba(255,255,255,0.08)',
                  background: selected
                    ? 'rgba(79,70,229,0.28)'
                    : inRange
                      ? 'rgba(79,70,229,0.16)'
                      : active
                        ? 'rgba(56,189,248,0.10)'
                        : reviewed
                          ? 'rgba(34,197,94,0.08)'
                          : 'rgba(15,18,28,0.9)',
                  cursor: 'pointer',
                }}
              >
                <Typography variant="caption" color="rgba(255,255,255,0.48)">{index + 1}</Typography>
                <Box sx={{ minWidth: 0 }}>
                  <Typography noWrap fontWeight={700}>{line.text || 'Пустая строка'}</Typography>
                  <Stack direction="row" spacing={0.75} alignItems="center" sx={{ mt: 0.5 }}>
                    <Chip size="small" label={statusLabel} color={statusColor} />
                    <Typography variant="caption" color="rgba(255,255,255,0.52)" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                      {formatTime(line.start)}
                    </Typography>
                  </Stack>
                </Box>
                <Stack direction="row" spacing={0.5}>
                  <Tooltip title="Слушать"><IconButton size="small" onClick={(event) => { event.stopPropagation(); onPlay(line); }} sx={{ color: 'white' }}><PlayArrowIcon fontSize="small" /></IconButton></Tooltip>
                  <Tooltip title="Удалить строку">
                    <IconButton
                      size="small"
                      onClick={(event) => { event.stopPropagation(); onDeleteLine(line); }}
                      sx={{ color: 'rgba(255,255,255,0.78)' }}
                    >
                      <DeleteIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                  <Tooltip title={reviewed ? 'Снять отметку' : 'Отметить как проверенную'}>
                    <IconButton
                      size="small"
                      onClick={(event) => { event.stopPropagation(); onMarkReviewed(line); }}
                      sx={{
                        color: reviewed ? '#86efac' : 'white',
                        background: reviewed ? 'rgba(34,197,94,0.16)' : 'transparent',
                        border: reviewed ? '1px solid rgba(34,197,94,0.28)' : '1px solid transparent',
                      }}
                    >
                      <CheckCircleIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                </Stack>
              </Box>
            );
          })}
        </Stack>
      </Box>
      <Typography variant="caption" color="rgba(255,255,255,0.5)" sx={{ display: 'block', mt: 1.25 }}>
        Показано {document.lines.length} из {document.lines.length} строк
      </Typography>
    </Paper>
  );
}

function LineTimeline({ document, selectedLine, selectedLineIds, activeLine, currentTime, lineZoom, duration, issues, onSelect, onSeek, onMove, onResize }: {
  document: AlignmentDocument;
  selectedLine: AlignmentLine | null;
  selectedLineIds: Set<string>;
  activeLine: AlignmentLine | null;
  currentTime: number;
  lineZoom: number;
  duration: number;
  issues: Issue[];
  onSelect: (line: AlignmentLine) => void;
  onSeek: (time: number) => void;
  onMove: (line: AlignmentLine, nextStart: number) => void;
  onResize: (line: AlignmentLine, start: number, end: number) => void;
}) {
  const timelineEnd = Math.max(duration, getDocumentEnd(document), 10);
  const width = Math.max(900, timelineEnd * lineZoom + 120);
  const playheadX = currentTime * lineZoom;
  const issueLineIds = new Set(issues.map((issue) => issue.lineId));
  return (
    <Paper sx={{ p: 2, background: 'rgba(255,255,255,0.06)', color: 'white' }}>
      <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 1 }}>
        <Typography variant="h6" fontWeight={700}>Timeline строк</Typography>
        <Typography variant="caption" color="rgba(255,255,255,0.6)">Drag = сдвиг, edges = start/end</Typography>
      </Stack>
      <Box sx={{ overflowX: 'auto', border: '1px solid rgba(255,255,255,0.12)', borderRadius: 1 }}>
        <Box sx={{ position: 'relative', width, height: 104, background: '#111118' }} onDoubleClick={(event) => onSeek((event.nativeEvent.offsetX || 0) / lineZoom)}>
          <Box sx={{ position: 'absolute', left: playheadX, top: 0, bottom: 0, width: 2, background: '#d7f45a', zIndex: 5 }} />
          {document.lines.map((line, index) => {
            const selected = selectedLine?.id === line.id;
            const inRange = selectedLineIds.has(line.id);
            const active = activeLine?.id === line.id;
            const x = Math.max(0, line.start * lineZoom);
            const blockWidth = Math.max(24, (line.end - line.start) * lineZoom);
            return (
              <Rnd key={line.id} size={{ width: blockWidth, height: LINE_BLOCK_HEIGHT }} position={{ x, y: 28 }} bounds="parent" dragAxis="x" enableResizing={{ left: true, right: true }} minWidth={Math.max(8, MIN_BLOCK_SEC * lineZoom)} style={{ zIndex: selected ? 4 : active ? 3 : 2 }} onMouseDown={() => onSelect(line)} onDoubleClick={() => onSeek(line.start)} onDragStop={(_, data) => onMove(line, Math.max(0, data.x / lineZoom))} onResizeStop={(_, __, ref, ___, position) => onResize(line, Math.max(0, position.x / lineZoom), Math.max(position.x / lineZoom + MIN_BLOCK_SEC, (position.x + ref.offsetWidth) / lineZoom))}>
                <Box sx={{ height: '100%', display: 'grid', gridTemplateColumns: blockWidth >= 150 ? '42px minmax(0, 1fr)' : '1fr', alignItems: 'center', gap: 1, px: 1, borderRadius: 1, border: selected ? '2px solid #d7f45a' : inRange ? '1px solid #d7f45a' : active ? '1px solid #80deea' : '1px solid rgba(255,255,255,0.18)', background: issueLineIds.has(line.id) ? 'rgba(234,179,8,0.22)' : selected ? 'rgba(215,244,90,0.18)' : inRange ? 'rgba(215,244,90,0.08)' : 'rgba(42,45,58,0.96)', cursor: 'grab', userSelect: 'none' }}>
                  {blockWidth >= 150 && <Typography variant="caption" noWrap color="rgba(255,255,255,0.58)">#{index + 1}</Typography>}
                  <Typography noWrap fontWeight={700}>{line.text}</Typography>
                </Box>
              </Rnd>
            );
          })}
        </Box>
      </Box>
    </Paper>
  );
}

function LineDetailEditor({ document, selectedLine, selectedSyllable, currentTime, detailZoom, onZoom, onSelectSyllable, onSeek, onMoveSyllable, onResizeSyllable }: {
  document: AlignmentDocument;
  selectedLine: AlignmentLine | null;
  selectedSyllable: AlignmentSyllable | null;
  currentTime: number;
  detailZoom: number;
  onZoom: (zoom: number) => void;
  onSelectSyllable: (syllable: AlignmentSyllable) => void;
  onSeek: (time: number) => void;
  onMoveSyllable: (syllable: AlignmentSyllable, nextStart: number) => void;
  onResizeSyllable: (syllable: AlignmentSyllable, start: number, end: number) => void;
}) {
  const syllables = selectedLine ? getLineSyllables(document, selectedLine) : [];
  const detailStart = selectedLine ? Math.max(0, selectedLine.start - 0.6) : 0;
  const detailEnd = selectedLine ? Math.max(selectedLine.end + 0.6, detailStart + 2) : 2;
  const detailWidth = Math.max(880, (detailEnd - detailStart) * detailZoom + 80);
  const playheadX = (currentTime - detailStart) * detailZoom;
  return (
    <Paper sx={{ p: 2, background: 'rgba(255,255,255,0.06)', color: 'white' }}>
      <Stack direction="row" alignItems="center" spacing={2} sx={{ mb: 1 }}>
        <Typography variant="h6" fontWeight={700}>Слоги выбранной строки</Typography>
        <Slider min={180} max={720} step={20} value={detailZoom} onChange={(_, value) => onZoom(value as number)} sx={{ width: 240 }} />
      </Stack>
      {!selectedLine ? <Alert severity="info">Выберите строку.</Alert> : (
        <Box sx={{ overflowX: 'auto', border: '1px solid rgba(255,255,255,0.12)', borderRadius: 1 }}>
          <Box sx={{ position: 'relative', width: detailWidth, height: SYLLABLE_ROW_HEIGHT, background: '#101018' }}>
            {playheadX >= 0 && playheadX <= detailWidth && <Box sx={{ position: 'absolute', left: playheadX, top: 0, bottom: 0, width: 2, background: '#d7f45a', zIndex: 5 }} />}
            {syllables.map((syllable) => {
              const selected = selectedSyllable?.id === syllable.id;
              const active = currentTime >= syllable.start && currentTime <= syllable.end;
              const x = Math.max(0, (syllable.start - detailStart) * detailZoom);
              const width = Math.max(24, (syllable.end - syllable.start) * detailZoom);
              const isBad = syllable.end - syllable.start < 0.03 || syllable.end - syllable.start > 2.5;
              return (
                <Rnd key={syllable.id} size={{ width, height: 50 }} position={{ x, y: 16 }} bounds="parent" dragAxis="x" enableResizing={{ left: true, right: true }} minWidth={Math.max(8, MIN_BLOCK_SEC * detailZoom)} onMouseDown={() => onSelectSyllable(syllable)} onDoubleClick={() => onSeek(syllable.start)} onDragStop={(_, data) => onMoveSyllable(syllable, detailStart + Math.max(0, data.x / detailZoom))} onResizeStop={(_, __, ref, ___, position) => onResizeSyllable(syllable, detailStart + Math.max(0, position.x / detailZoom), Math.max(detailStart + position.x / detailZoom + MIN_BLOCK_SEC, detailStart + (position.x + ref.offsetWidth) / detailZoom))}>
                  <Box sx={{ height: '100%', display: 'grid', alignItems: 'center', px: 1, borderRadius: 1, border: selected ? '2px solid #d7f45a' : active ? '1px solid #80deea' : '1px solid rgba(255,255,255,0.18)', background: isBad ? 'rgba(239,68,68,0.24)' : selected ? 'rgba(215,244,90,0.18)' : 'rgba(49,55,74,0.96)', cursor: 'grab', userSelect: 'none', overflow: 'hidden' }}>
                    <Typography noWrap fontWeight={800}>{syllable.text}</Typography>
                    <Typography variant="caption" color="rgba(255,255,255,0.62)" sx={{ fontVariantNumeric: 'tabular-nums' }}>{formatTime(syllable.start)}-{formatTime(syllable.end)}</Typography>
                  </Box>
                </Rnd>
              );
            })}
          </Box>
        </Box>
      )}
    </Paper>
  );
}

function InspectorPanel({ selectedLine, selectedSyllable, selectedLineIssues, currentTime, onPlayRange, onSelectLine, onApply, onMarkReviewed }: {
  selectedLine: AlignmentLine | null;
  selectedSyllable: AlignmentSyllable | null;
  selectedLineIssues: Issue[];
  currentTime: number;
  onPlayRange: (start: number, end: number, leadIn?: number) => Promise<void>;
  onSelectLine: () => void;
  onApply: (type: string, updater: (current: AlignmentDocument) => AlignmentDocument, options?: { lineId?: string; syllableId?: string; selection?: Selection | null; reviewState?: Record<string, ReviewStatus>; payload?: Record<string, unknown> }) => void;
  onMarkReviewed: () => void;
}) {
  const selected = selectedSyllable ?? selectedLine;
  return (
    <Paper sx={{ p: 2, background: 'rgba(12,16,28,0.92)', color: 'white', alignSelf: 'start', borderRadius: 3, border: '1px solid rgba(255,255,255,0.08)' }}>
      <Typography variant="h6" fontWeight={700}>Инспектор</Typography>
      <Divider sx={{ my: 2, borderColor: 'rgba(255,255,255,0.12)' }} />
      {!selectedLine ? <Typography color="rgba(255,255,255,0.6)">Выберите строку или слог.</Typography> : selectedSyllable ? (
        <Stack spacing={2}>
          <Chip label="Слог выбран" />
          <TextField label="Text" value={selectedSyllable.text} size="small" onChange={(event) => onApply('UPDATE_SYLLABLE_TEXT', (current) => updateSyllableText(current, selectedSyllable.id, event.target.value), { syllableId: selectedSyllable.id })} InputProps={{ sx: { color: 'white' } }} InputLabelProps={{ sx: { color: 'rgba(255,255,255,0.7)' } }} />
          <TimingFields start={selectedSyllable.start} end={selectedSyllable.end} />
          <NudgeControls onNudge={(delta) => onApply('NUDGE_SYLLABLE', (current) => shiftSyllable(current, selectedSyllable.id, delta), { syllableId: selectedSyllable.id })} />
          <Stack direction="row" spacing={1} flexWrap="wrap">
            <Button onClick={() => void onPlayRange(selectedSyllable.start, selectedSyllable.end)}>Play</Button>
            <Button onClick={() => onApply('SET_SYLLABLE_START_TO_PLAYHEAD', (current) => updateSyllableTiming(current, selectedSyllable.id, currentTime, selectedSyllable.end), { syllableId: selectedSyllable.id })}>Начало сюда</Button>
            <Button onClick={() => onApply('SET_SYLLABLE_END_TO_PLAYHEAD', (current) => updateSyllableTiming(current, selectedSyllable.id, selectedSyllable.start, currentTime), { syllableId: selectedSyllable.id })}>Конец сюда</Button>
          </Stack>
          <Stack direction="row" spacing={1} flexWrap="wrap">
            <Button startIcon={<AddIcon />} onClick={() => onApply('INSERT_SYLLABLE', (current) => insertSyllable(current, selectedSyllable.line_id, selectedSyllable.id, currentTime), { lineId: selectedSyllable.line_id })}>Insert</Button>
            <Button startIcon={<ContentCutIcon />} onClick={() => onApply('SPLIT_SYLLABLE', (current) => splitSyllable(current, selectedSyllable.id), { syllableId: selectedSyllable.id })}>Split</Button>
            <Button startIcon={<CallMergeIcon />} onClick={() => onApply('MERGE_SYLLABLE_WITH_NEXT', (current) => mergeSyllableWithNext(current, selectedSyllable.id), { syllableId: selectedSyllable.id })}>Merge next</Button>
            <Button startIcon={<DeleteIcon />} color="error" onClick={() => window.confirm('Удалить слог?') && onApply('DELETE_SYLLABLE', (current) => deleteSyllable(current, selectedSyllable.id), { syllableId: selectedSyllable.id, selection: { type: 'line', id: selectedSyllable.line_id } })}>Delete</Button>
          </Stack>
          <Button onClick={onSelectLine}>Вернуться к строке</Button>
        </Stack>
      ) : (
        <Stack spacing={2}>
          <TimingStepperRow label="Начало" value={selectedLine.start} onMinus={() => onApply('NUDGE_LINE_START_BACK', (current) => stretchLine(current, selectedLine.id, Math.max(0, selectedLine.start - NUDGE_SMALL), selectedLine.end), { lineId: selectedLine.id })} onPlus={() => onApply('NUDGE_LINE_START_FORWARD', (current) => stretchLine(current, selectedLine.id, Math.min(selectedLine.end - MIN_BLOCK_SEC, selectedLine.start + NUDGE_SMALL), selectedLine.end), { lineId: selectedLine.id })} />
          <TimingStepperRow label="Конец" value={selectedLine.end} onMinus={() => onApply('NUDGE_LINE_END_BACK', (current) => stretchLine(current, selectedLine.id, selectedLine.start, Math.max(selectedLine.start + MIN_BLOCK_SEC, selectedLine.end - NUDGE_SMALL)), { lineId: selectedLine.id })} onPlus={() => onApply('NUDGE_LINE_END_FORWARD', (current) => stretchLine(current, selectedLine.id, selectedLine.start, selectedLine.end + NUDGE_SMALL), { lineId: selectedLine.id })} />
          <Box>
            <Typography variant="caption" color="rgba(255,255,255,0.55)">Длительность</Typography>
            <Typography sx={{ mt: 0.5, fontVariantNumeric: 'tabular-nums', fontSize: 28, fontWeight: 700 }}>{formatTime(selectedLine.end - selectedLine.start)} сек</Typography>
          </Box>
          <Accordion elevation={0} sx={{ background: 'rgba(255,255,255,0.03)', color: 'white', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '16px !important' }}>
            <AccordionSummary expandIcon={<ExpandMoreIcon sx={{ color: 'white' }} />}>
              <Typography fontWeight={700}>Дополнительно</Typography>
            </AccordionSummary>
            <AccordionDetails>
              <Stack spacing={1.25}>
                <TextField label="Текст строки" value={selectedLine.text} size="small" multiline onChange={(event) => onApply('UPDATE_LINE_TEXT', (current) => updateLineText(current, selectedLine.id, event.target.value), { lineId: selectedLine.id })} InputProps={{ sx: { color: 'white' } }} InputLabelProps={{ sx: { color: 'rgba(255,255,255,0.7)' } }} />
                <Stack direction="row" spacing={1} flexWrap="wrap">
                  <Button onClick={() => void onPlayRange(selectedLine.start, selectedLine.end)}>Слушать</Button>
                  <Button onClick={() => onApply('SET_LINE_START_TO_PLAYHEAD', (current) => stretchLine(current, selectedLine.id, currentTime, selectedLine.end), { lineId: selectedLine.id })}>Начало = сейчас</Button>
                  <Button onClick={() => onApply('SET_LINE_END_TO_PLAYHEAD', (current) => stretchLine(current, selectedLine.id, selectedLine.start, currentTime), { lineId: selectedLine.id })}>Конец = сейчас</Button>
                </Stack>
                <NudgeControls onNudge={(delta) => onApply('NUDGE_LINE', (current) => shiftLine(current, selectedLine.id, delta), { lineId: selectedLine.id })} />
                <Stack direction="row" spacing={1} flexWrap="wrap">
                  <Button startIcon={<AddIcon />} onClick={() => onApply('CREATE_LINE_AFTER', (current) => createLineAfter(current, selectedLine.id, currentTime), { lineId: selectedLine.id })}>Добавить строку</Button>
                  <Button startIcon={<AddIcon />} onClick={() => onApply('INSERT_SYLLABLE', (current) => insertSyllable(current, selectedLine.id, null, currentTime), { lineId: selectedLine.id })}>Добавить слог</Button>
                  <Button startIcon={<ContentCopyIcon />} onClick={() => onApply('DUPLICATE_LINE', (current) => duplicateLine(current, selectedLine.id, currentTime), { lineId: selectedLine.id })}>Дублировать</Button>
                  <Button startIcon={<DeleteIcon />} color="error" onClick={() => window.confirm('Удалить строку?') && onApply('DELETE_LINE', (current) => deleteLine(current, selectedLine.id), { lineId: selectedLine.id, selection: null })}>Удалить</Button>
                </Stack>
                <Stack direction="row" spacing={1} flexWrap="wrap">
                  <Button startIcon={<KeyboardArrowUpIcon />} onClick={() => onApply('MOVE_LINE_UP', (current) => moveLine(current, selectedLine.id, -1), { lineId: selectedLine.id })}>Вверх</Button>
                  <Button startIcon={<KeyboardArrowDownIcon />} onClick={() => onApply('MOVE_LINE_DOWN', (current) => moveLine(current, selectedLine.id, 1), { lineId: selectedLine.id })}>Вниз</Button>
                  <Button startIcon={<ContentCutIcon />} onClick={() => onApply('SPLIT_LINE_AT_PLAYHEAD', (current) => splitLine(current, selectedLine.id, currentTime), { lineId: selectedLine.id })}>Разделить</Button>
                  <Button startIcon={<CallMergeIcon />} onClick={() => onApply('MERGE_LINE_WITH_NEXT', (current) => mergeLineWithNext(current, selectedLine.id), { lineId: selectedLine.id })}>Слить со следующей</Button>
                </Stack>
              </Stack>
            </AccordionDetails>
          </Accordion>
          <Button startIcon={<SkipNextIcon />} variant="contained" onClick={onMarkReviewed}>Проверено и дальше</Button>
          {selectedLineIssues.length > 0 && <Stack direction="row" spacing={0.5} flexWrap="wrap">{selectedLineIssues.map((issue) => <Chip key={issue.id} size="small" color={issue.severity === 'critical' ? 'error' : 'warning'} label={issue.label} />)}</Stack>}
        </Stack>
      )}
      {selected && <Typography variant="caption" color="rgba(255,255,255,0.58)" sx={{ mt: 2, display: 'block' }}>Duration: {formatTime(selected.end - selected.start)} sec</Typography>}
    </Paper>
  );
}

function TimingStepperRow({ label, value, onMinus, onPlus }: { label: string; value: number; onMinus: () => void; onPlus: () => void }) {
  return (
    <Box>
      <Typography variant="caption" color="rgba(255,255,255,0.55)">{label}</Typography>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mt: 0.5 }}>
        <TextField value={formatTime(value)} size="small" fullWidth InputProps={{ readOnly: true, sx: { color: 'white', fontVariantNumeric: 'tabular-nums' } }} />
        <IconButton size="small" onClick={onMinus} sx={{ color: 'white', border: '1px solid rgba(255,255,255,0.12)' }}>
          <ZoomOutIcon fontSize="small" />
        </IconButton>
        <IconButton size="small" onClick={onPlus} sx={{ color: 'white', border: '1px solid rgba(255,255,255,0.12)' }}>
          <AddIcon fontSize="small" />
        </IconButton>
      </Stack>
    </Box>
  );
}

function ReviewSummaryPanel({
  issueCount,
  unreviewedCount,
  skippedCount,
  reviewedCount,
  progressPercent,
  onShowAll,
}: {
  issueCount: number;
  unreviewedCount: number;
  skippedCount: number;
  reviewedCount: number;
  progressPercent: number;
  onShowAll: () => void;
}) {
  return (
    <Paper sx={{ p: 2, borderRadius: 3, background: 'rgba(12,16,28,0.92)', color: 'white', border: '1px solid rgba(255,255,255,0.08)' }}>
      <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 1.5 }}>
        <Typography variant="h6" fontWeight={700}>Очередь проверки</Typography>
        <Button size="small" onClick={onShowAll}>Показать все</Button>
      </Stack>
      <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 1 }}>
        <SummaryMetric label="Проблем" value={issueCount} tone="warning" />
        <SummaryMetric label="На проверке" value={unreviewedCount} tone="amber" />
        <SummaryMetric label="Пропущено" value={skippedCount} tone="neutral" />
        <SummaryMetric label="Проверено" value={reviewedCount} tone="success" />
      </Box>
      <Typography variant="caption" color="rgba(255,255,255,0.55)" sx={{ display: 'block', mt: 1.5 }}>Прогресс</Typography>
      <Box sx={{ mt: 0.75, height: 8, borderRadius: 999, background: 'rgba(255,255,255,0.08)', overflow: 'hidden' }}>
        <Box sx={{ width: `${progressPercent}%`, height: '100%', background: 'linear-gradient(90deg, #7c3aed, #8b5cf6)' }} />
      </Box>
      <Typography variant="caption" color="rgba(255,255,255,0.62)" sx={{ display: 'block', mt: 0.75 }}>{progressPercent}%</Typography>
    </Paper>
  );
}

function SummaryMetric({ label, value, tone }: { label: string; value: number; tone: 'warning' | 'amber' | 'neutral' | 'success' }) {
  const colors = {
    warning: { background: 'rgba(124,58,237,0.2)', color: '#c4b5fd' },
    amber: { background: 'rgba(245,158,11,0.16)', color: '#fbbf24' },
    neutral: { background: 'rgba(255,255,255,0.06)', color: '#e5e7eb' },
    success: { background: 'rgba(34,197,94,0.14)', color: '#86efac' },
  }[tone];

  return (
    <Box sx={{ p: 1.25, borderRadius: 2, background: colors.background }}>
      <Typography sx={{ fontSize: 28, fontWeight: 800, color: colors.color, lineHeight: 1 }}>{value}</Typography>
      <Typography variant="caption" color="rgba(255,255,255,0.62)">{label}</Typography>
    </Box>
  );
}

function TimingFields({ start, end }: { start: number; end: number }) {
  return (
    <Stack direction="row" spacing={1}>
      <TextField label="Start" value={formatTime(start)} size="small" InputProps={{ readOnly: true, sx: { color: 'white' } }} />
      <TextField label="End" value={formatTime(end)} size="small" InputProps={{ readOnly: true, sx: { color: 'white' } }} />
    </Stack>
  );
}

function NudgeControls({ onNudge }: { onNudge: (delta: number) => void }) {
  return (
    <Stack direction="row" spacing={1} flexWrap="wrap">
      <Button onClick={() => onNudge(-NUDGE_LARGE)}>-250ms</Button>
      <Button onClick={() => onNudge(-NUDGE_SMALL)}>-50ms</Button>
      <Button onClick={() => onNudge(NUDGE_SMALL)}>+50ms</Button>
      <Button onClick={() => onNudge(NUDGE_LARGE)}>+250ms</Button>
    </Stack>
  );
}

function PublishPreflightDialog({ open, criticalIssues, warningIssues, unresolvedCount, onClose, onPublishAnyway }: {
  open: boolean;
  criticalIssues: Issue[];
  warningIssues: Issue[];
  unresolvedCount: number;
  onClose: () => void;
  onPublishAnyway: () => void;
}) {
  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="sm">
      <DialogTitle>Проверка перед публикацией</DialogTitle>
      <DialogContent>
        {criticalIssues.length > 0 && <Alert severity="error" sx={{ mb: 2 }}>Критичные ошибки: {criticalIssues.length}. Публикация заблокирована до исправления.</Alert>}
        {warningIssues.length > 0 && <Alert severity="warning" sx={{ mb: 2 }}>Предупреждения: {warningIssues.length}. Их можно опубликовать осознанно.</Alert>}
        {unresolvedCount > 0 && <Alert severity="info">Непроверенные строки: {unresolvedCount}.</Alert>}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Назад</Button>
        <Button variant="contained" disabled={criticalIssues.length > 0} onClick={onPublishAnyway}>Опубликовать</Button>
      </DialogActions>
    </Dialog>
  );
}

function RevisionHistoryDrawer({ open, revisions, activeRevisionId, onClose, onRestore, onRequestAdminSecret }: {
  open: boolean;
  revisions: AlignmentRevision[];
  activeRevisionId: string | null;
  onClose: () => void;
  onRestore: (revisionId: string) => Promise<void>;
  onRequestAdminSecret: () => Promise<boolean>;
}) {
  const [restoringId, setRestoringId] = useState<string | null>(null);
  return (
    <Drawer anchor="right" open={open} onClose={onClose} PaperProps={{ sx: { width: 420, background: '#11111a', color: 'white', p: 2 } }}>
      <Typography variant="h6" fontWeight={700}>История версий</Typography>
      <Stack spacing={1.5} sx={{ mt: 2 }}>
        {revisions.map((revision) => (
          <Paper key={revision.id} sx={{ p: 1.5, background: revision.id === activeRevisionId ? 'rgba(215,244,90,0.14)' : 'rgba(255,255,255,0.06)', color: 'white' }}>
            <Stack direction="row" justifyContent="space-between" alignItems="center">
              <Box>
                <Typography fontWeight={700}>Revision #{revision.revision_no}</Typography>
                <Typography variant="caption" color="rgba(255,255,255,0.62)">{revision.source} · {revision.created_at}</Typography>
              </Box>
              <Chip size="small" label={revision.is_published ? 'Опубликовано' : 'Черновик'} color={revision.is_published ? 'success' : 'default'} />
            </Stack>
            <Button size="small" sx={{ mt: 1 }} disabled={revision.id === activeRevisionId || restoringId === revision.id} onClick={async () => {
              const granted = await onRequestAdminSecret();
              if (!granted) return;
              setRestoringId(revision.id);
              await onRestore(revision.id);
              setRestoringId(null);
            }}>Восстановить</Button>
          </Paper>
        ))}
      </Stack>
    </Drawer>
  );
}
