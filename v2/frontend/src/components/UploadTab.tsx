import React, { useState, useRef, useCallback, useEffect } from 'react';
import {
  Box,
  Typography,
  InputBase,
  LinearProgress,
  ButtonBase,
} from '@mui/material';
import CloudUploadIcon from '@mui/icons-material/CloudUpload';
import FolderOpenIcon from '@mui/icons-material/FolderOpen';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import ErrorOutlineIcon from '@mui/icons-material/ErrorOutline';
import AddIcon from '@mui/icons-material/Add';

import { api } from '../services/api';
import { subscribeToJobStatus } from '../services/sseService';
import type { JobStatusEvent } from '../types';

// ─── Constants ────────────────────────────────────────────────────────────────

const MAX_FILE_SIZE_MB = 50;
const ACCEPTED_TYPES = '.mp3,.wav,.m4a,audio/mpeg,audio/wav,audio/x-m4a,audio/mp4';

const STEP_LABELS: Record<string, string> = {
  separating: 'Разделение вокала и музыки',
  transcribing: 'Распознавание текста',
  generating_video: 'Создание караоке-видео',
  extracting_features: 'Анализ музыки',
  embedding_lyrics: 'Обработка текста',
  syncing_qdrant: 'Индексация',
};

// ─── Types ────────────────────────────────────────────────────────────────────

type UploadPhase =
  | { kind: 'idle' }
  | { kind: 'uploading'; progress: number }
  | { kind: 'processing'; step: string; progress: number }
  | { kind: 'done'; trackId: string }
  | { kind: 'error'; message: string };

// ─── Helpers ──────────────────────────────────────────────────────────────────

function formatFileSize(bytes: number): string {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function isValidFileSize(file: File): boolean {
  return file.size <= MAX_FILE_SIZE_MB * 1024 * 1024;
}

// ─── Props ────────────────────────────────────────────────────────────────────

interface UploadTabProps {
  sessionId: string;
  selectedParticipantId: string | null;
  onTrackUploaded: (trackId: string) => void;
}

// ─── UploadTab ────────────────────────────────────────────────────────────────

export const UploadTab: React.FC<UploadTabProps> = ({
  selectedParticipantId,
  onTrackUploaded,
}) => {
  const [file, setFile] = useState<File | null>(null);
  const [artist, setArtist] = useState('');
  const [title, setTitle] = useState('');
  const [isDragOver, setIsDragOver] = useState(false);
  const [phase, setPhase] = useState<UploadPhase>({ kind: 'idle' });
  const [fileSizeError, setFileSizeError] = useState<string | null>(null);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const unsubscribeRef = useRef<(() => void) | null>(null);

  // Cleanup SSE on unmount
  useEffect(() => {
    return () => {
      unsubscribeRef.current?.();
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
    setPhase({ kind: 'idle' });
  }, []);

  const handleFileInputChange = (e: React.ChangeEvent<HTMLInputElement>): void => {
    const picked = e.target.files?.[0];
    if (picked) acceptFile(picked);
    // Reset input so same file can be picked again after a reset
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

  // ── Upload ───────────────────────────────────────────────────────────────

  const handleUpload = useCallback(async (): Promise<void> => {
    if (!file) return;

    setPhase({ kind: 'uploading', progress: 0 });

    let uploadResponse: { track_id: string; job_id: string; status: string };

    try {
      uploadResponse = await api.uploadTrack(
        file,
        artist.trim() || undefined,
        title.trim() || undefined
      );
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Ошибка загрузки';
      setPhase({ kind: 'error', message });
      return;
    }

    // Start processing phase while SSE streams
    setPhase({ kind: 'processing', step: 'Начинаем обработку...', progress: 0 });

    const { job_id, track_id } = uploadResponse;

    unsubscribeRef.current?.();

    const unsubscribe = subscribeToJobStatus(
      job_id,
      (event: JobStatusEvent) => {
        if (event.status === 'completed') {
          unsubscribeRef.current?.();
          setPhase({ kind: 'done', trackId: event.track_id ?? track_id });
        } else if (event.status === 'error') {
          unsubscribeRef.current?.();
          setPhase({ kind: 'error', message: event.error ?? 'Ошибка при обработке' });
        } else {
          // status event with step/progress
          const stepLabel = event.step ? (STEP_LABELS[event.step] ?? event.step) : 'Обработка...';
          setPhase({ kind: 'processing', step: stepLabel, progress: event.progress ?? 0 });
        }
      },
      () => {
        // SSE connection error — only treat as error if not already done
        setPhase((prev) => {
          if (prev.kind === 'done') return prev;
          return { kind: 'error', message: 'Соединение прервано' };
        });
      }
    );

    unsubscribeRef.current = unsubscribe;
  }, [file, artist, title]);

  const handleReset = (): void => {
    unsubscribeRef.current?.();
    unsubscribeRef.current = null;
    setFile(null);
    setArtist('');
    setTitle('');
    setPhase({ kind: 'idle' });
    setFileSizeError(null);
  };

  const handleAddToQueue = (): void => {
    if (phase.kind !== 'done') return;
    onTrackUploaded(phase.trackId);
  };

  // ── Derived ──────────────────────────────────────────────────────────────

  const isActive = phase.kind !== 'idle';
  const canUpload = file !== null && !isActive;

  // ── Render ───────────────────────────────────────────────────────────────

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 3, position: 'relative' }}>

      {/* Drag & Drop Zone */}
      <Box
        onClick={!isActive ? handleZoneClick : undefined}
        onDrop={!isActive ? handleDrop : undefined}
        onDragOver={!isActive ? handleDragOver : undefined}
        onDragLeave={!isActive ? handleDragLeave : undefined}
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
          cursor: !isActive ? 'pointer' : 'default',
          transition: 'border-color 0.2s ease, background 0.2s ease, box-shadow 0.2s ease',
          boxShadow: isDragOver
            ? '0 0 32px rgba(6,182,212,0.2), inset 0 0 40px rgba(6,182,212,0.06)'
            : 'none',
          position: 'relative',
          overflow: 'hidden',
        }}
      >
        {/* Overlay when active */}
        {isActive && (
          <Box
            sx={{
              position: 'absolute',
              inset: 0,
              background: 'rgba(10,5,30,0.88)',
              backdropFilter: 'blur(6px)',
              borderRadius: '18px',
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'center',
              gap: 2.5,
              zIndex: 2,
              px: 4,
            }}
          >
            {/* Phase: uploading */}
            {phase.kind === 'uploading' && (
              <>
                <Typography sx={{ fontSize: '15px', fontWeight: 600, color: '#A78BFA' }}>
                  Загрузка...
                </Typography>
                <Box sx={{ width: '100%', maxWidth: 320 }}>
                  <LinearProgress
                    variant="indeterminate"
                    sx={{
                      borderRadius: '4px',
                      height: 6,
                      backgroundColor: 'rgba(255,255,255,0.08)',
                      '& .MuiLinearProgress-bar': {
                        background: 'linear-gradient(90deg, #06B6D4, #7C3AED)',
                        borderRadius: '4px',
                      },
                    }}
                  />
                </Box>
              </>
            )}

            {/* Phase: processing */}
            {phase.kind === 'processing' && (
              <>
                <Typography sx={{ fontSize: '13px', fontWeight: 700, letterSpacing: '0.08em', color: 'rgba(255,255,255,0.4)', textTransform: 'uppercase' }}>
                  Создаём Karaoke
                </Typography>
                <Typography sx={{ fontSize: '15px', fontWeight: 600, color: '#67E8F9', textAlign: 'center' }}>
                  {phase.step}
                </Typography>
                <Box sx={{ width: '100%', maxWidth: 320 }}>
                  <LinearProgress
                    variant={phase.progress > 0 ? 'determinate' : 'indeterminate'}
                    value={phase.progress}
                    sx={{
                      borderRadius: '4px',
                      height: 6,
                      backgroundColor: 'rgba(255,255,255,0.08)',
                      '& .MuiLinearProgress-bar': {
                        background: 'linear-gradient(90deg, #06B6D4, #7C3AED)',
                        borderRadius: '4px',
                      },
                    }}
                  />
                </Box>
                {phase.progress > 0 && (
                  <Typography sx={{ fontSize: '12px', color: 'rgba(255,255,255,0.3)' }}>
                    {phase.progress}%
                  </Typography>
                )}
              </>
            )}

            {/* Phase: done */}
            {phase.kind === 'done' && (
              <>
                <CheckCircleIcon sx={{ fontSize: 48, color: '#10B981' }} />
                <Typography sx={{ fontSize: '16px', fontWeight: 700, color: '#FFFFFF' }}>
                  Готово к исполнению!
                </Typography>
                <ButtonBase
                  onClick={handleAddToQueue}
                  disabled={!selectedParticipantId}
                  sx={{
                    px: 3,
                    py: 1.25,
                    borderRadius: '24px',
                    background: 'linear-gradient(135deg, #06B6D4, #7C3AED)',
                    color: '#FFFFFF',
                    fontSize: '13px',
                    fontWeight: 700,
                    letterSpacing: '0.08em',
                    textTransform: 'uppercase',
                    display: 'flex',
                    alignItems: 'center',
                    gap: 0.75,
                    boxShadow: '0 4px 20px rgba(6,182,212,0.35)',
                    transition: 'opacity 0.2s ease',
                    '&:hover': { opacity: 0.88 },
                    '&:disabled': { opacity: 0.4, cursor: 'not-allowed' },
                  }}
                >
                  <AddIcon sx={{ fontSize: 18 }} />
                  ДОБАВИТЬ В ОЧЕРЕДЬ
                </ButtonBase>
                <ButtonBase
                  onClick={handleReset}
                  sx={{
                    fontSize: '12px',
                    color: 'rgba(255,255,255,0.35)',
                    textDecoration: 'underline',
                    '&:hover': { color: 'rgba(255,255,255,0.6)' },
                  }}
                >
                  Загрузить ещё один трек
                </ButtonBase>
              </>
            )}

            {/* Phase: error */}
            {phase.kind === 'error' && (
              <>
                <ErrorOutlineIcon sx={{ fontSize: 44, color: '#F87171' }} />
                <Typography sx={{ fontSize: '14px', fontWeight: 600, color: '#FCA5A5', textAlign: 'center', maxWidth: 280 }}>
                  {phase.message}
                </Typography>
                <ButtonBase
                  onClick={handleReset}
                  sx={{
                    px: 2.5,
                    py: 1,
                    borderRadius: '20px',
                    background: 'rgba(248,113,113,0.2)',
                    border: '1px solid rgba(248,113,113,0.4)',
                    color: '#FCA5A5',
                    fontSize: '12px',
                    fontWeight: 700,
                    letterSpacing: '0.08em',
                    textTransform: 'uppercase',
                    transition: 'background 0.2s ease',
                    '&:hover': { background: 'rgba(248,113,113,0.3)' },
                  }}
                >
                  Попробовать снова
                </ButtonBase>
              </>
            )}
          </Box>
        )}

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
              MP3, WAV, M4A — до {MAX_FILE_SIZE_MB} МБ
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
        disabled={isActive}
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
          '&:disabled': {
            opacity: 0.35,
            cursor: 'not-allowed',
          },
        }}
      >
        <FolderOpenIcon sx={{ fontSize: 18 }} />
        ВЫБРАТЬ ФАЙЛ
      </ButtonBase>

      {/* Metadata fields */}
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
          ДЕТАЛИ ТРЕКА (необязательно)
        </Typography>

        <Box sx={{ display: 'flex', gap: 1.5 }}>
          {/* Artist field */}
          <Box
            sx={{
              flex: 1,
              display: 'flex',
              alignItems: 'center',
              height: 44,
              borderRadius: '12px',
              background: 'rgba(255,255,255,0.05)',
              border: '1px solid rgba(255,255,255,0.1)',
              px: 1.75,
              transition: 'border-color 0.2s ease',
              '&:focus-within': {
                borderColor: 'rgba(6,182,212,0.45)',
              },
            }}
          >
            <InputBase
              value={artist}
              onChange={(e) => setArtist(e.target.value)}
              placeholder="напр. Кино"
              disabled={isActive}
              fullWidth
              sx={{
                color: '#FFFFFF',
                fontSize: '14px',
                '& input': {
                  padding: 0,
                  '&::placeholder': {
                    color: 'rgba(255,255,255,0.25)',
                    opacity: 1,
                  },
                },
              }}
            />
          </Box>

          {/* Title field */}
          <Box
            sx={{
              flex: 1,
              display: 'flex',
              alignItems: 'center',
              height: 44,
              borderRadius: '12px',
              background: 'rgba(255,255,255,0.05)',
              border: '1px solid rgba(255,255,255,0.1)',
              px: 1.75,
              transition: 'border-color 0.2s ease',
              '&:focus-within': {
                borderColor: 'rgba(6,182,212,0.45)',
              },
            }}
          >
            <InputBase
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="напр. Группа крови"
              disabled={isActive}
              fullWidth
              sx={{
                color: '#FFFFFF',
                fontSize: '14px',
                '& input': {
                  padding: 0,
                  '&::placeholder': {
                    color: 'rgba(255,255,255,0.25)',
                    opacity: 1,
                  },
                },
              }}
            />
          </Box>
        </Box>
      </Box>

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
    </Box>
  );
};
