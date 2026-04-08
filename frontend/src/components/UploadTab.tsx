import React, { useState, useRef, useCallback, useEffect } from 'react';
import {
  Box,
  Typography,
  LinearProgress,
  ButtonBase,
  IconButton,
} from '@mui/material';
import CloudUploadIcon from '@mui/icons-material/CloudUpload';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import ErrorOutlineIcon from '@mui/icons-material/ErrorOutline';
import CloseIcon from '@mui/icons-material/Close';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';

import { api } from '../services/api';
import { subscribeToJobStatus } from '../services/sseService';
import type { JobStatusEvent } from '../types';

// ─── Constants ────────────────────────────────────────────────────────────────

const MAX_FILE_SIZE_MB = 50;
const ACCEPTED_TYPES = '.mp3,audio/mpeg';

const STEP_LABELS: Record<string, string> = {
  separating: 'Разделение вокала и музыки',
  transcribing: 'Распознавание текста',
  extracting_features: 'Анализ музыки',
  embedding_lyrics: 'Обработка текста',
  syncing_qdrant: 'Индексация',
};

// ─── Types ────────────────────────────────────────────────────────────────────

type JobPhase =
  | { kind: 'uploading' }
  | { kind: 'processing'; step: string; progress: number }
  | { kind: 'done'; trackId: string }
  | { kind: 'error'; message: string };

interface UploadJob {
  id: string;
  fileName: string;
  phase: JobPhase;
  createdAt: number;
  backendJobId?: string;
  trackId?: string;
  unsubscribe?: () => void;
}

/** Serializable subset of UploadJob for sessionStorage persistence. */
interface PersistedJob {
  id: string;
  fileName: string;
  phase: JobPhase;
  createdAt: number;
  backendJobId?: string;
  trackId?: string;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function formatFileSize(bytes: number): string {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function isValidFileSize(file: File): boolean {
  return file.size <= MAX_FILE_SIZE_MB * 1024 * 1024;
}

function storageKey(sessionId: string): string {
  return `uploadJobs_${sessionId}`;
}

function loadPersistedJobs(sessionId: string): UploadJob[] {
  try {
    const raw = sessionStorage.getItem(storageKey(sessionId));
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

function persistJobs(sessionId: string, jobs: UploadJob[]): void {
  try {
    const serializable: PersistedJob[] = jobs.map(({ unsubscribe: _, ...rest }) => rest);
    sessionStorage.setItem(storageKey(sessionId), JSON.stringify(serializable));
  } catch {
    // sessionStorage unavailable
  }
}


// ─── Props ────────────────────────────────────────────────────────────────────

interface UploadTabProps {
  sessionId: string;
  onPlay: (trackId: string) => void;
  compact?: boolean;
}

// ─── JobCard ──────────────────────────────────────────────────────────────────

const JobCard: React.FC<{
  job: UploadJob;
  onDismiss: (id: string) => void;
  onPlay?: (trackId: string) => void;
}> = ({ job, onDismiss, onPlay }) => {
  const { phase } = job;
  return (
    <Box
      sx={{
        display: 'flex',
        alignItems: 'center',
        gap: 1.5,
        px: 2,
        py: 1.5,
        borderRadius: '14px',
        background: 'rgba(255,255,255,0.04)',
        border: `1px solid ${
          phase.kind === 'done'
            ? 'rgba(16,185,129,0.3)'
            : phase.kind === 'error'
              ? 'rgba(248,113,113,0.3)'
              : 'rgba(6,182,212,0.2)'
        }`,
      }}
    >
      {/* Icon */}
      <Box sx={{ flexShrink: 0, display: 'flex', alignItems: 'center' }}>
        {phase.kind === 'done' && <CheckCircleIcon sx={{ fontSize: 22, color: '#10B981' }} />}
        {phase.kind === 'error' && <ErrorOutlineIcon sx={{ fontSize: 22, color: '#F87171' }} />}
        {(phase.kind === 'uploading' || phase.kind === 'processing') && (
          <CloudUploadIcon sx={{ fontSize: 22, color: '#67E8F9' }} />
        )}
      </Box>

      {/* Content */}
      <Box sx={{ flex: 1, minWidth: 0 }}>
        <Typography
          sx={{
            fontSize: '13px',
            fontWeight: 600,
            color: '#FFFFFF',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {job.fileName}
        </Typography>

        {phase.kind === 'uploading' && (
          <Typography sx={{ fontSize: '11px', color: '#A78BFA' }}>
            Загрузка...
          </Typography>
        )}

        {phase.kind === 'processing' && (
          <Box>
            <Typography sx={{ fontSize: '11px', color: '#67E8F9' }}>
              {phase.step}
            </Typography>
            <LinearProgress
              variant={phase.progress > 0 ? 'determinate' : 'indeterminate'}
              value={phase.progress}
              sx={{
                mt: 0.5,
                borderRadius: '4px',
                height: 4,
                backgroundColor: 'rgba(255,255,255,0.08)',
                '& .MuiLinearProgress-bar': {
                  background: 'linear-gradient(90deg, #06B6D4, #7C3AED)',
                  borderRadius: '4px',
                },
              }}
            />
          </Box>
        )}

        {phase.kind === 'done' && (
          <Typography sx={{ fontSize: '11px', color: '#10B981' }}>
            Готово
          </Typography>
        )}

        {phase.kind === 'error' && (
          <Typography sx={{ fontSize: '11px', color: '#FCA5A5' }}>
            {phase.message}
          </Typography>
        )}
      </Box>

      {/* Play button for done */}
      {phase.kind === 'done' && onPlay && (
        <IconButton
          size="small"
          onClick={() => onPlay(phase.trackId)}
          sx={{
            width: 32,
            height: 32,
            background: 'linear-gradient(135deg, #7C3AED, #2563EB)',
            color: '#FFFFFF',
            '&:hover': { opacity: 0.85 },
          }}
        >
          <PlayArrowIcon sx={{ fontSize: 18 }} />
        </IconButton>
      )}

      {/* Dismiss button for error */}
      {phase.kind === 'error' && (
        <IconButton
          size="small"
          onClick={() => onDismiss(job.id)}
          sx={{ color: 'rgba(255,255,255,0.3)', '&:hover': { color: 'rgba(255,255,255,0.6)' } }}
        >
          <CloseIcon sx={{ fontSize: 16 }} />
        </IconButton>
      )}
    </Box>
  );
};

// ─── UploadTab ────────────────────────────────────────────────────────────────

export const UploadTab: React.FC<UploadTabProps> = ({
  sessionId,
  onPlay,
  compact = false,
}) => {
  const [file, setFile] = useState<File | null>(null);
  const [isDragOver, setIsDragOver] = useState(false);
  const [fileSizeError, setFileSizeError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [jobs, setJobs] = useState<UploadJob[]>(() => loadPersistedJobs(sessionId));

  const fileInputRef = useRef<HTMLInputElement>(null);
  const jobsRef = useRef<UploadJob[]>([]);
  jobsRef.current = jobs;

  // Persist jobs to sessionStorage on every change
  useEffect(() => {
    persistJobs(sessionId, jobs);
  }, [jobs, sessionId]);

  // ── Subscribe a job to SSE ──────────────────────────────────────────────

  const subscribeJob = useCallback((localJobId: string, backendJobId: string, trackId?: string) => {
    let unsub: (() => void) | undefined;

    const closeStream = () => {
      if (unsub) { unsub(); unsub = undefined; }
    };

    unsub = subscribeToJobStatus(
      backendJobId,
      (event: JobStatusEvent) => {
        setJobs((prev) => prev.map((j) => {
          if (j.id !== localJobId) return j;
          if (j.phase.kind === 'done' || j.phase.kind === 'error') return j;

          if (event.status === 'completed') {
            closeStream();
            const finalTrackId = event.track_id ?? trackId;
            return { ...j, phase: { kind: 'done' as const, trackId: finalTrackId! }, trackId: finalTrackId, unsubscribe: undefined };
          }
          if (event.status === 'error') {
            closeStream();
            return { ...j, phase: { kind: 'error' as const, message: event.error ?? 'Ошибка при обработке' }, unsubscribe: undefined };
          }
          const stepLabel = event.step ? (STEP_LABELS[event.step] ?? event.step) : 'Обработка...';
          return { ...j, phase: { kind: 'processing' as const, step: stepLabel, progress: event.progress ?? 0 } };
        }));
      },
      () => {
        setJobs((prev) => prev.map((j) => {
          if (j.id !== localJobId) return j;
          if (j.phase.kind === 'done' || j.phase.kind === 'error') return j;
          return { ...j, phase: { kind: 'error' as const, message: 'Соединение прервано' }, unsubscribe: undefined };
        }));
      }
    );
    setJobs((prev) => prev.map((j) =>
      j.id === localJobId ? { ...j, unsubscribe: closeStream } : j
    ));
  }, []);

  // ── Restore: re-subscribe persisted processing jobs from sessionStorage ──

  useEffect(() => {
    for (const j of jobsRef.current) {
      if (j.phase.kind === 'processing' && j.backendJobId && !j.unsubscribe) {
        subscribeJob(j.id, j.backendJobId, j.trackId);
      }
    }
  }, [subscribeJob]);

  // Cleanup SSE on unmount
  useEffect(() => {
    return () => {
      jobsRef.current.forEach((j) => j.unsubscribe?.());
    };
  }, []);

  // ── File handling ────────────────────────────────────────────────────────

  const acceptFile = useCallback((incoming: File): void => {
    if (!isValidFileSize(incoming)) {
      setFileSizeError(`Файл слишком большой: ${formatFileSize(incoming.size)}. Максимум ${MAX_FILE_SIZE_MB} МБ.`);
      return;
    }
    setFileSizeError(null);
    setFile(incoming);
  }, []);

  const handleFileInputChange = (e: React.ChangeEvent<HTMLInputElement>): void => {
    const picked = e.target.files?.[0];
    if (picked) acceptFile(picked);
    e.target.value = '';
  };

  const handleDrop = useCallback((e: React.DragEvent<HTMLDivElement>): void => {
    e.preventDefault();
    setIsDragOver(false);
    const dropped = e.dataTransfer.files?.[0];
    if (dropped) acceptFile(dropped);
  }, [acceptFile]);

  const handleDragOver = (e: React.DragEvent<HTMLDivElement>): void => {
    e.preventDefault();
    setIsDragOver(true);
  };

  const handleDragLeave = (e: React.DragEvent<HTMLDivElement>): void => {
    e.preventDefault();
    setIsDragOver(false);
  };

  const handleZoneClick = (): void => {
    fileInputRef.current?.click();
  };

  // ── Job state helpers ─────────────────────────────────────────────────────

  const updateJob = useCallback((jobId: string, patch: Partial<UploadJob>) => {
    setJobs((prev) => prev.map((j) => (j.id === jobId ? { ...j, ...patch } : j)));
  }, []);

  const dismissJob = useCallback((jobId: string) => {
    setJobs((prev) => {
      const job = prev.find((j) => j.id === jobId);
      job?.unsubscribe?.();
      return prev.filter((j) => j.id !== jobId);
    });
  }, []);

  // ── Upload ───────────────────────────────────────────────────────────────

  const handleUpload = useCallback(async (): Promise<void> => {
    if (!file || uploading) return;

    const localJobId = crypto.randomUUID();
    const fileName = file.name;
    const newJob: UploadJob = {
      id: localJobId,
      fileName,
      phase: { kind: 'uploading' },
      createdAt: Date.now(),
    };
    setJobs((prev) => [newJob, ...prev]);

    setFile(null);
    setUploading(true);

    let uploadResponse: { track_id: string; job_id: string; status: string };

    try {
      uploadResponse = await api.uploadTrack(file);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Ошибка загрузки';
      updateJob(localJobId, { phase: { kind: 'error', message } });
      setUploading(false);
      return;
    }

    setUploading(false);

    const { job_id, track_id } = uploadResponse;

    updateJob(localJobId, {
      phase: { kind: 'processing', step: 'Начинаем обработку...', progress: 0 },
      backendJobId: job_id,
      trackId: track_id,
    });

    subscribeJob(localJobId, job_id, track_id);
  }, [file, uploading, updateJob, subscribeJob]);

  // ── Derived ──────────────────────────────────────────────────────────────

  const canUpload = file !== null && !uploading;

  // ── Auto-upload on file select in compact mode ──────────────────────────

  useEffect(() => {
    if (compact && file && !uploading) {
      void handleUpload();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [file, compact]);

  // ── Render ───────────────────────────────────────────────────────────────

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: compact ? 2 : 3, position: 'relative', minHeight: 0, flex: 1 }}>

      {/* Drag & Drop Zone */}
      <Box
        onClick={handleZoneClick}
        onDrop={handleDrop}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        sx={{
          height: compact ? 120 : 240,
          borderRadius: compact ? '16px' : '20px',
          border: `2px dashed ${isDragOver ? 'rgba(6,182,212,0.9)' : 'rgba(6,182,212,0.4)'}`,
          background: isDragOver
            ? 'rgba(6,182,212,0.08)'
            : 'rgba(255,255,255,0.03)',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: compact ? 1 : 1.5,
          cursor: 'pointer',
          transition: 'border-color 0.2s ease, background 0.2s ease, box-shadow 0.2s ease',
          boxShadow: isDragOver
            ? '0 0 32px rgba(6,182,212,0.2), inset 0 0 40px rgba(6,182,212,0.06)'
            : 'none',
          position: 'relative',
          overflow: 'hidden',
        }}
      >
        {!compact && (
          <Box
            sx={{
              width: 72,
              height: 72,
              borderRadius: '50%',
              background: 'linear-gradient(135deg, rgba(6,182,212,0.2), rgba(124,58,237,0.2))',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
            }}
          >
            <CloudUploadIcon
              sx={{
                fontSize: 36,
                background: 'linear-gradient(135deg, #06B6D4, #7C3AED)',
                WebkitBackgroundClip: 'text',
                WebkitTextFillColor: 'transparent',
                backgroundClip: 'text',
              }}
            />
          </Box>
        )}

        {compact ? (
          <>
            <CloudUploadIcon
              sx={{
                fontSize: 28,
                color: isDragOver ? '#67E8F9' : 'rgba(6,182,212,0.7)',
              }}
            />
            <Typography sx={{ fontSize: '13px', fontWeight: 600, color: isDragOver ? '#67E8F9' : 'rgba(255,255,255,0.6)', textAlign: 'center', px: 1 }}>
              {isDragOver ? 'Отпустите!' : 'Перетащите MP3 или нажмите'}
            </Typography>
            <Typography sx={{ fontSize: '11px', color: 'rgba(255,255,255,0.25)' }}>
              до {MAX_FILE_SIZE_MB} МБ
            </Typography>
          </>
        ) : file ? (
          <>
            <Typography sx={{ fontSize: '15px', fontWeight: 600, color: '#FFFFFF' }}>
              {file.name}
            </Typography>
            <Typography sx={{ fontSize: '12px', color: 'rgba(255,255,255,0.4)' }}>
              {formatFileSize(file.size)} — нажмите чтобы сменить файл
            </Typography>
          </>
        ) : (
          <>
            <Typography sx={{ fontSize: '15px', fontWeight: 600, color: isDragOver ? '#67E8F9' : 'rgba(255,255,255,0.7)' }}>
              {isDragOver ? 'Отпустите здесь!' : 'Перетащите MP3 сюда'}
            </Typography>
            <Typography sx={{ fontSize: '12px', color: 'rgba(255,255,255,0.3)' }}>
              MP3 — до {MAX_FILE_SIZE_MB} МБ
            </Typography>
          </>
        )}
      </Box>

      {fileSizeError && (
        <Typography sx={{ fontSize: compact ? '11px' : '13px', color: '#FCA5A5', textAlign: 'center' }}>
          {fileSizeError}
        </Typography>
      )}

      <input
        ref={fileInputRef}
        type="file"
        accept={ACCEPTED_TYPES}
        onChange={handleFileInputChange}
        style={{ display: 'none' }}
      />

      {!compact && (
        <>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
            <Box sx={{ flex: 1, height: '1px', background: 'rgba(255,255,255,0.08)' }} />
            <Typography sx={{ fontSize: '12px', color: 'rgba(255,255,255,0.25)', letterSpacing: '0.08em' }}>
              — ИЛИ —
            </Typography>
            <Box sx={{ flex: 1, height: '1px', background: 'rgba(255,255,255,0.08)' }} />
          </Box>

          <ButtonBase
            onClick={handleZoneClick}
            sx={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: 1,
              py: 1.5,
              borderRadius: '14px',
              border: '1px solid rgba(6,182,212,0.4)',
              color: '#67E8F9',
              fontSize: '13px',
              fontWeight: 700,
              letterSpacing: '0.08em',
              textTransform: 'uppercase',
              transition: 'background 0.2s ease, border-color 0.2s ease',
              '&:hover': {
                background: 'rgba(6,182,212,0.1)',
                borderColor: 'rgba(6,182,212,0.7)',
              },
            }}
          >
            ВЫБРАТЬ ФАЙЛ
          </ButtonBase>

          <ButtonBase
            onClick={() => { void handleUpload(); }}
            disabled={!canUpload}
            sx={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: 1,
              py: 1.75,
              borderRadius: '14px',
              background: canUpload
                ? 'linear-gradient(135deg, #06B6D4, #7C3AED)'
                : 'rgba(255,255,255,0.06)',
              color: canUpload ? '#FFFFFF' : 'rgba(255,255,255,0.25)',
              fontSize: '13px',
              fontWeight: 700,
              letterSpacing: '0.1em',
              textTransform: 'uppercase',
              boxShadow: canUpload ? '0 4px 24px rgba(6,182,212,0.3)' : 'none',
              transition: 'opacity 0.2s ease, box-shadow 0.2s ease',
              '&:hover': canUpload ? { opacity: 0.88 } : {},
              '&:disabled': {
                cursor: 'not-allowed',
              },
            }}
          >
            <CloudUploadIcon sx={{ fontSize: 18 }} />
            ЗАГРУЗИТЬ И СОЗДАТЬ KARAOKE
          </ButtonBase>
        </>
      )}

      {/* Upload job cards */}
      {jobs.length > 0 && (
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, flex: 1, minHeight: 0 }}>
          <Typography
            sx={{
              fontSize: '11px',
              fontWeight: 700,
              letterSpacing: '0.12em',
              color: 'rgba(255,255,255,0.3)',
              textTransform: 'uppercase',
              flexShrink: 0,
            }}
          >
            МОИ ЗАГРУЗКИ
          </Typography>
          <Box sx={{ flex: 1, overflowY: 'auto', minHeight: 0, display: 'flex', flexDirection: 'column', gap: 1.5 }}>
            {[...jobs]
              .sort((a, b) => b.createdAt - a.createdAt)
              .map((job) => (
                <JobCard key={job.id} job={job} onDismiss={dismissJob} onPlay={onPlay} />
              ))}
          </Box>
        </Box>
      )}
    </Box>
  );
};
