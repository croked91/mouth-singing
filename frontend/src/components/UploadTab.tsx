import React, { useState, useRef, useCallback, useEffect } from 'react';
import {
  Box,
  Typography,
  LinearProgress,
  ButtonBase,
  IconButton,
} from '@mui/material';
import CloudUploadIcon from '@mui/icons-material/CloudUpload';
import FolderOpenIcon from '@mui/icons-material/FolderOpen';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import ErrorOutlineIcon from '@mui/icons-material/ErrorOutline';
import CloseIcon from '@mui/icons-material/Close';

import { api } from '../services/api';
import { subscribeToJobStatus } from '../services/sseService';
import type { ActiveJob, JobStatusEvent } from '../types';

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
  /** Backend job ID for SSE subscription */
  backendJobId?: string;
  /** Track ID returned by upload endpoint */
  trackId?: string;
  unsubscribe?: () => void;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function formatFileSize(bytes: number): string {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function isValidFileSize(file: File): boolean {
  return file.size <= MAX_FILE_SIZE_MB * 1024 * 1024;
}

/** Convert an ActiveJob from the backend into a local UploadJob. */
function activeJobToUploadJob(aj: ActiveJob): UploadJob {
  const stepLabel = aj.current_step
    ? (STEP_LABELS[aj.current_step] ?? aj.current_step)
    : 'Обработка...';
  return {
    id: `backend-${aj.job_id}`,
    fileName: `${aj.artist} — ${aj.title}`,
    phase: { kind: 'processing', step: stepLabel, progress: aj.progress },
    backendJobId: aj.job_id,
    trackId: aj.track_id,
  };
}

let jobCounter = 0;

// ─── Props ────────────────────────────────────────────────────────────────────

interface UploadTabProps {
  sessionId: string;
  onTrackUploaded: (trackId: string) => void;
}

// ─── JobCard ──────────────────────────────────────────────────────────────────

const JobCard: React.FC<{
  job: UploadJob;
  onDismiss: (id: string) => void;
}> = ({ job, onDismiss }) => {
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
            Готово, добавлен в очередь
          </Typography>
        )}

        {phase.kind === 'error' && (
          <Typography sx={{ fontSize: '11px', color: '#FCA5A5' }}>
            {phase.message}
          </Typography>
        )}
      </Box>

      {/* Dismiss button for done/error */}
      {(phase.kind === 'done' || phase.kind === 'error') && (
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
  onTrackUploaded,
}) => {
  const [file, setFile] = useState<File | null>(null);
  const [isDragOver, setIsDragOver] = useState(false);
  const [fileSizeError, setFileSizeError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [jobs, setJobs] = useState<UploadJob[]>([]);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const jobsRef = useRef<UploadJob[]>([]);
  jobsRef.current = jobs;
  const onTrackUploadedRef = useRef(onTrackUploaded);
  onTrackUploadedRef.current = onTrackUploaded;

  // ── Subscribe a job to SSE ──────────────────────────────────────────────

  const subscribeJob = useCallback((localJobId: string, backendJobId: string, trackId?: string) => {
    const unsubscribe = subscribeToJobStatus(
      backendJobId,
      (event: JobStatusEvent) => {
        if (event.status === 'completed') {
          const finalTrackId = event.track_id ?? trackId;
          setJobs((prev) => prev.map((j) =>
            j.id === localJobId
              ? { ...j, phase: { kind: 'done' as const, trackId: finalTrackId! }, unsubscribe: undefined }
              : j
          ));
          if (finalTrackId) onTrackUploadedRef.current(finalTrackId);
        } else if (event.status === 'error') {
          setJobs((prev) => prev.map((j) =>
            j.id === localJobId
              ? { ...j, phase: { kind: 'error' as const, message: event.error ?? 'Ошибка при обработке' }, unsubscribe: undefined }
              : j
          ));
        } else {
          const stepLabel = event.step ? (STEP_LABELS[event.step] ?? event.step) : 'Обработка...';
          setJobs((prev) => prev.map((j) =>
            j.id === localJobId
              ? { ...j, phase: { kind: 'processing' as const, step: stepLabel, progress: event.progress ?? 0 } }
              : j
          ));
        }
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
      j.id === localJobId ? { ...j, unsubscribe } : j
    ));
  }, []);

  // ── Restore active jobs from backend on mount ───────────────────────────

  useEffect(() => {
    let cancelled = false;

    void api.getActiveJobs().then((activeJobs) => {
      if (cancelled || activeJobs.length === 0) return;

      // Only add jobs not already tracked locally
      setJobs((prev) => {
        const existingBackendIds = new Set(prev.map((j) => j.backendJobId).filter(Boolean));
        const newJobs = activeJobs
          .filter((aj) => !existingBackendIds.has(aj.job_id))
          .map(activeJobToUploadJob);
        return [...newJobs, ...prev];
      });

      // Subscribe each restored job to SSE
      for (const aj of activeJobs) {
        const localId = `backend-${aj.job_id}`;
        // Check not already subscribed
        const existing = jobsRef.current.find((j) => j.id === localId);
        if (existing?.unsubscribe) continue;
        subscribeJob(localId, aj.job_id, aj.track_id);
      }
    }).catch(() => { /* network error — ignore, jobs just won't appear */ });

    return () => { cancelled = true; };
  }, [subscribeJob]);

  // Cleanup all SSE subscriptions on unmount
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

    const localJobId = `local-${++jobCounter}`;
    const fileName = file.name;
    // Add job card immediately
    const newJob: UploadJob = {
      id: localJobId,
      fileName,
      phase: { kind: 'uploading' },
    };
    setJobs((prev) => [newJob, ...prev]);

    // Reset form so user can upload another
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

    // Start processing phase (track is added to queue only after processing completes)
    updateJob(localJobId, {
      phase: { kind: 'processing', step: 'Начинаем обработку...', progress: 0 },
      backendJobId: job_id,
      trackId: track_id,
    });

    subscribeJob(localJobId, job_id, track_id);
  }, [file, uploading, updateJob, subscribeJob]);

  // ── Derived ──────────────────────────────────────────────────────────────

  const canUpload = file !== null && !uploading;

  // ── Render ───────────────────────────────────────────────────────────────

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 3, position: 'relative' }}>

      {/* Drag & Drop Zone */}
      <Box
        onClick={handleZoneClick}
        onDrop={handleDrop}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        sx={{
          height: 240,
          borderRadius: '20px',
          border: `2px dashed ${isDragOver ? 'rgba(6,182,212,0.9)' : 'rgba(6,182,212,0.4)'}`,
          background: isDragOver
            ? 'rgba(6,182,212,0.08)'
            : 'rgba(255,255,255,0.03)',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 1.5,
          cursor: 'pointer',
          transition: 'border-color 0.2s ease, background 0.2s ease, box-shadow 0.2s ease',
          boxShadow: isDragOver
            ? '0 0 32px rgba(6,182,212,0.2), inset 0 0 40px rgba(6,182,212,0.06)'
            : 'none',
          position: 'relative',
          overflow: 'hidden',
        }}
      >
        {/* Default zone content */}
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

        {file ? (
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

      {/* File size error */}
      {fileSizeError && (
        <Typography sx={{ fontSize: '13px', color: '#FCA5A5', textAlign: 'center' }}>
          {fileSizeError}
        </Typography>
      )}

      {/* Hidden file input */}
      <input
        ref={fileInputRef}
        type="file"
        accept={ACCEPTED_TYPES}
        onChange={handleFileInputChange}
        style={{ display: 'none' }}
      />

      {/* Divider */}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
        <Box sx={{ flex: 1, height: '1px', background: 'rgba(255,255,255,0.08)' }} />
        <Typography sx={{ fontSize: '12px', color: 'rgba(255,255,255,0.25)', letterSpacing: '0.08em' }}>
          — ИЛИ —
        </Typography>
        <Box sx={{ flex: 1, height: '1px', background: 'rgba(255,255,255,0.08)' }} />
      </Box>

      {/* Choose file button */}
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
        <FolderOpenIcon sx={{ fontSize: 18 }} />
        ВЫБРАТЬ ФАЙЛ
      </ButtonBase>

      {/* Upload button */}
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

      {/* Active/completed job cards */}
      {jobs.length > 0 && (
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
          <Typography
            sx={{
              fontSize: '11px',
              fontWeight: 700,
              letterSpacing: '0.12em',
              color: 'rgba(255,255,255,0.3)',
              textTransform: 'uppercase',
            }}
          >
            ЗАГРУЗКИ
          </Typography>
          {jobs.map((job) => (
            <JobCard key={job.id} job={job} onDismiss={dismissJob} />
          ))}
        </Box>
      )}
    </Box>
  );
};
