import React, {
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react';
import { useNavigate, useParams, useLocation } from 'react-router-dom';
import {
  Box,
  CircularProgress,
  IconButton,
  Slider,
  Tooltip,
  Typography,
} from '@mui/material';
import StopIcon from '@mui/icons-material/Stop';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';
import PauseIcon from '@mui/icons-material/Pause';
import Replay10Icon from '@mui/icons-material/Replay10';
import Forward10Icon from '@mui/icons-material/Forward10';
import VolumeUpIcon from '@mui/icons-material/VolumeUp';
import VolumeDownIcon from '@mui/icons-material/VolumeDown';

import { api } from '../../services/api';
import { LyricHighlight } from '../../components/LyricHighlight';
import type { StartPlayingResponse } from '../../types';

// ─── Constants ────────────────────────────────────────────────────────────────

const SEEK_STEP_SEC = 10;

// ─── Helpers ──────────────────────────────────────────────────────────────────

function formatTime(seconds: number): string {
  if (!isFinite(seconds) || seconds < 0) return '0:00';
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}

// ─── Component ────────────────────────────────────────────────────────────────

export const PlayerPage: React.FC = () => {
  const { id: sessionId, entryId } = useParams<{ id: string; entryId: string }>();
  const navigate = useNavigate();
  const location = useLocation();

  // startData may be passed via navigation state from the main page
  const navStartData = (location.state as { startData?: StartPlayingResponse } | null)?.startData ?? null;

  // ── API response state ──────────────────────────────────────────────────────
  const [startData, setStartData] = useState<StartPlayingResponse | null>(navStartData);
  const [isLoading, setIsLoading] = useState(navStartData === null);
  const [loadError, setLoadError] = useState<string | null>(null);

  // ── Audio playback state ────────────────────────────────────────────────────
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [volume, setVolume] = useState(() => {
    const saved = localStorage.getItem('karaoke_volume');
    return saved !== null ? Number(saved) : 1;
  });

  // rAF loop for progress slider
  const sliderRafRef = useRef<number | null>(null);
  const sliderBarRef = useRef<HTMLSpanElement | null>(null);
  const timeDisplayRef = useRef<HTMLSpanElement | null>(null);

  const isFinishingRef = useRef(false);

  // ── Load data on mount ──────────────────────────────────────────────────────

  useEffect(() => {
    if (!entryId || startData) return;

    let cancelled = false;

    const load = async (): Promise<void> => {
      try {
        const startRes = await api.startPlaying(entryId);
        if (cancelled) return;
        setStartData(startRes);
      } catch (err) {
        if (!cancelled) {
          setLoadError(err instanceof Error ? err.message : 'Ошибка запуска');
        }
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    };

    void load();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entryId]);

  // ── Wire audio element once track_id is known ────────────────────────────────

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio || !startData?.clip_url) return;

    audio.src = startData.clip_url;
    audio.volume = volume;
    audio.load();

    // Autoplay — browser allows it after recent user gesture ("ПЕТЬ" click).
    // If blocked, silently ignore — user can press play manually.
    const handleCanPlay = (): void => {
      audio.removeEventListener('canplay', handleCanPlay);
      audio.play().catch(() => {});
    };
    audio.addEventListener('canplay', handleCanPlay);

    return () => {
      audio.removeEventListener('canplay', handleCanPlay);
      audio.pause();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [startData?.clip_url]);

  // ── Audio event listeners ───────────────────────────────────────────────────
  // Re-run when isLoading changes so listeners attach after <audio> is mounted.

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;

    const handleTimeUpdate = (): void => {
      setCurrentTime(audio.currentTime);
    };

    const handleDurationChange = (): void => {
      setDuration(audio.duration);
    };

    const handlePlay = (): void => setIsPlaying(true);
    const handlePause = (): void => setIsPlaying(false);

    const handleEnded = (): void => {
      setIsPlaying(false);
      void handleFinish();
    };

    audio.addEventListener('timeupdate', handleTimeUpdate);
    audio.addEventListener('durationchange', handleDurationChange);
    audio.addEventListener('loadedmetadata', handleDurationChange);
    audio.addEventListener('play', handlePlay);
    audio.addEventListener('pause', handlePause);
    audio.addEventListener('ended', handleEnded);

    return () => {
      audio.removeEventListener('timeupdate', handleTimeUpdate);
      audio.removeEventListener('durationchange', handleDurationChange);
      audio.removeEventListener('loadedmetadata', handleDurationChange);
      audio.removeEventListener('play', handlePlay);
      audio.removeEventListener('pause', handlePause);
      audio.removeEventListener('ended', handleEnded);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isLoading]);

  // ── rAF loop: update time display and slider without React state ──────────────

  useEffect(() => {
    const tick = (): void => {
      const audio = audioRef.current;
      if (!audio) return;

      // Update DOM elements directly for performance
      if (timeDisplayRef.current) {
        timeDisplayRef.current.textContent = formatTime(audio.currentTime);
      }
      if (sliderBarRef.current && isFinite(audio.duration) && audio.duration > 0) {
        const pct = (audio.currentTime / audio.duration) * 100;
        sliderBarRef.current.style.width = `${pct}%`;
      }

      if (isPlaying) {
        sliderRafRef.current = requestAnimationFrame(tick);
      }
    };

    if (isPlaying) {
      sliderRafRef.current = requestAnimationFrame(tick);
    } else {
      if (sliderRafRef.current !== null) {
        cancelAnimationFrame(sliderRafRef.current);
        sliderRafRef.current = null;
      }
    }

    return () => {
      if (sliderRafRef.current !== null) {
        cancelAnimationFrame(sliderRafRef.current);
        sliderRafRef.current = null;
      }
    };
  }, [isPlaying]);

  // ── Handlers ─────────────────────────────────────────────────────────────────

  const handlePlayPause = useCallback((): void => {
    const audio = audioRef.current;
    if (!audio) return;
    if (audio.paused) {
      void audio.play();
    } else {
      audio.pause();
    }
  }, []);

  const handleRewind = useCallback((): void => {
    const audio = audioRef.current;
    if (!audio) return;
    audio.currentTime = Math.max(0, audio.currentTime - SEEK_STEP_SEC);
    setCurrentTime(audio.currentTime);
  }, []);

  const handleForward = useCallback((): void => {
    const audio = audioRef.current;
    if (!audio) return;
    audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + SEEK_STEP_SEC);
    setCurrentTime(audio.currentTime);
  }, []);

  const handleSliderChange = useCallback(
    (_: Event, value: number | number[]): void => {
      const audio = audioRef.current;
      if (!audio) return;
      const newTime = Array.isArray(value) ? value[0] : value;
      audio.currentTime = newTime;
      setCurrentTime(newTime);
    },
    []
  );

  const handleVolumeChange = useCallback(
    (_: Event, value: number | number[]): void => {
      const audio = audioRef.current;
      if (!audio) return;
      const vol = Array.isArray(value) ? value[0] : value;
      audio.volume = vol;
      setVolume(vol);
      localStorage.setItem('karaoke_volume', String(vol));
    },
    []
  );

  const handleFinish = useCallback(async (): Promise<void> => {
    if (isFinishingRef.current || !entryId || !sessionId) return;
    isFinishingRef.current = true;

    audioRef.current?.pause();

    try {
      await api.finishPlaying(entryId);
    } catch {
      // Navigate regardless — best-effort finish call
    } finally {
      navigate(`/session/${sessionId}`);
    }
  }, [entryId, sessionId, navigate]);

  // ── getCurrentTime ref-based accessor for LyricHighlight ─────────────────────

  const getCurrentTime = useCallback((): number => {
    return audioRef.current?.currentTime ?? 0;
  }, []);

  // ─── Loading state ────────────────────────────────────────────────────────────

  if (isLoading) {
    return (
      <Box
        sx={{
          position: 'fixed',
          inset: 0,
          backgroundColor: '#050508',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 3,
        }}
      >
        <CircularProgress size={56} sx={{ color: '#7C3AED' }} />
        <Typography sx={{ fontSize: '16px', color: 'rgba(255,255,255,0.5)' }}>
          Загрузка плеера...
        </Typography>
      </Box>
    );
  }

  if (loadError) {
    return (
      <Box
        sx={{
          position: 'fixed',
          inset: 0,
          backgroundColor: '#050508',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 2,
        }}
      >
        <Typography sx={{ fontSize: '20px', color: '#F87171' }}>
          Ошибка запуска
        </Typography>
        <Typography sx={{ fontSize: '14px', color: 'rgba(255,255,255,0.4)' }}>
          {loadError}
        </Typography>
      </Box>
    );
  }

  // ─── Derived values ────────────────────────────────────────────────────────────

  const syllableTimings = startData?.syllable_timings ?? null;

  // ─── Render ────────────────────────────────────────────────────────────────────

  return (
    <Box
      sx={{
        position: 'fixed',
        inset: 0,
        backgroundColor: '#050508',
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
      }}
    >
      {/* Hidden audio element */}
      <audio ref={audioRef} preload="auto" />

      {/* ── Background gradient blobs ── */}
      <Box
        sx={{
          position: 'absolute',
          inset: 0,
          pointerEvents: 'none',
          zIndex: 0,
          overflow: 'hidden',
        }}
      >
        {/* Left blob — deep violet */}
        <Box
          sx={{
            position: 'absolute',
            top: '50%',
            left: '-200px',
            transform: 'translateY(-50%)',
            width: '700px',
            height: '700px',
            borderRadius: '50%',
            background: 'radial-gradient(circle, rgba(76,29,149,0.4) 0%, transparent 70%)',
            filter: 'blur(80px)',
            '@keyframes blobDriftLeft': {
              '0%, 100%': { transform: 'translateY(-50%) scale(1)' },
              '50%': { transform: 'translateY(-45%) scale(1.05)' },
            },
            animation: 'blobDriftLeft 12s ease-in-out infinite',
          }}
        />
        {/* Right blob — deep blue */}
        <Box
          sx={{
            position: 'absolute',
            top: '50%',
            right: '-200px',
            transform: 'translateY(-50%)',
            width: '750px',
            height: '750px',
            borderRadius: '50%',
            background: 'radial-gradient(circle, rgba(30,58,95,0.35) 0%, transparent 70%)',
            filter: 'blur(90px)',
            '@keyframes blobDriftRight': {
              '0%, 100%': { transform: 'translateY(-50%) scale(1)' },
              '50%': { transform: 'translateY(-55%) scale(1.08)' },
            },
            animation: 'blobDriftRight 15s ease-in-out infinite',
          }}
        />
      </Box>

      {/* ── Top bar (64px) ── */}
      <Box
        sx={{
          position: 'relative',
          zIndex: 2,
          height: 64,
          flexShrink: 0,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          px: '32px',
          borderBottom: '1px solid rgba(255,255,255,0.06)',
        }}
      >
        {/* Left: track info */}
        <Box>
          <Typography
            sx={{
              fontWeight: 700,
              fontSize: '20px',
              color: '#FFFFFF',
              lineHeight: 1.2,
              maxWidth: '600px',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {startData?.title ?? '—'}
          </Typography>
          <Typography
            sx={{
              fontWeight: 400,
              fontSize: '18px',
              color: 'rgba(255,255,255,0.55)',
              lineHeight: 1.2,
              maxWidth: '600px',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {startData?.artist ?? ''}
          </Typography>
        </Box>

        {/* Right: ЗАВЕРШИТЬ button */}
        <Box
          component="button"
          onClick={() => { void handleFinish(); }}
          sx={{
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
            px: '20px',
            py: '8px',
            borderRadius: '24px',
            background: 'rgba(239,68,68,0.15)',
            border: '1px solid rgba(239,68,68,0.4)',
            color: '#F87171',
            fontSize: '13px',
            fontWeight: 700,
            letterSpacing: '0.08em',
            textTransform: 'uppercase',
            cursor: 'pointer',
            transition: 'all 0.2s ease',
            '&:hover': {
              background: 'rgba(239,68,68,0.25)',
              borderColor: 'rgba(239,68,68,0.65)',
            },
          }}
        >
          <StopIcon sx={{ fontSize: 18 }} />
          ЗАВЕРШИТЬ
        </Box>
      </Box>

      {/* ── Lyrics area — fills remaining height ── */}
      <Box
        sx={{
          position: 'relative',
          zIndex: 1,
          flex: 1,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          overflow: 'hidden',
        }}
      >
        {syllableTimings !== null && syllableTimings.length > 0 ? (
          <LyricHighlight
            syllableTimings={syllableTimings}
            getCurrentTime={getCurrentTime}
            isPlaying={isPlaying}
          />
        ) : (
          <Typography
            sx={{
              fontSize: '28px',
              fontWeight: 500,
              color: 'rgba(255,255,255,0.3)',
              letterSpacing: '0.04em',
            }}
          >
            Субтитры недоступны
          </Typography>
        )}
      </Box>

      {/* ── Bottom controls bar (80px) ── */}
      <Box
        sx={{
          position: 'relative',
          zIndex: 2,
          height: 80,
          flexShrink: 0,
          background:
            'linear-gradient(to top, rgba(5,5,8,0.97) 0%, transparent 100%)',
          display: 'flex',
          alignItems: 'center',
          px: '32px',
          gap: '24px',
        }}
      >
        {/* Current time */}
        <Typography
          component="span"
          ref={timeDisplayRef}
          sx={{
            fontFamily: '"Inter", sans-serif',
            fontWeight: 500,
            fontSize: '14px',
            color: 'rgba(255,255,255,0.5)',
            minWidth: '36px',
            flexShrink: 0,
            fontVariantNumeric: 'tabular-nums',
          }}
        >
          {formatTime(currentTime)}
        </Typography>

        {/* Rewind -10s */}
        <Tooltip title="-10 секунд" placement="top">
          <IconButton
            onClick={handleRewind}
            sx={{
              width: 44,
              height: 44,
              color: 'rgba(255,255,255,0.7)',
              '&:hover': {
                backgroundColor: 'rgba(255,255,255,0.08)',
                color: '#FFFFFF',
              },
            }}
          >
            <Replay10Icon sx={{ fontSize: 24 }} />
          </IconButton>
        </Tooltip>

        {/* Play / Pause */}
        <IconButton
          onClick={handlePlayPause}
          sx={{
            width: 56,
            height: 56,
            backgroundColor: 'rgba(255,255,255,0.1)',
            color: '#FFFFFF',
            flexShrink: 0,
            '&:hover': {
              backgroundColor: 'rgba(255,255,255,0.18)',
            },
          }}
        >
          {isPlaying ? (
            <PauseIcon sx={{ fontSize: 28 }} />
          ) : (
            <PlayArrowIcon sx={{ fontSize: 28 }} />
          )}
        </IconButton>

        {/* Forward +10s */}
        <Tooltip title="+10 секунд" placement="top">
          <IconButton
            onClick={handleForward}
            sx={{
              width: 44,
              height: 44,
              color: 'rgba(255,255,255,0.7)',
              '&:hover': {
                backgroundColor: 'rgba(255,255,255,0.08)',
                color: '#FFFFFF',
              },
            }}
          >
            <Forward10Icon sx={{ fontSize: 24 }} />
          </IconButton>
        </Tooltip>

        {/* Progress slider */}
        <Box sx={{ flex: 1, display: 'flex', alignItems: 'center' }}>
          <Slider
            value={currentTime}
            min={0}
            max={duration || 0}
            step={0.1}
            onChange={handleSliderChange}
            sx={{
              color: 'transparent',
              height: 4,
              padding: '12px 0',
              '& .MuiSlider-rail': {
                backgroundColor: 'rgba(255,255,255,0.12)',
                height: 4,
                borderRadius: 2,
              },
              '& .MuiSlider-track': {
                background: 'linear-gradient(90deg, #7C3AED, #06B6D4)',
                boxShadow: '0 0 8px rgba(124,58,237,0.5)',
                height: 4,
                border: 'none',
                borderRadius: 2,
              },
              '& .MuiSlider-thumb': {
                width: 16,
                height: 16,
                backgroundColor: '#FFFFFF',
                boxShadow: '0 0 8px rgba(255,255,255,0.4)',
                '&:hover, &.Mui-focusVisible': {
                  boxShadow: '0 0 0 8px rgba(255,255,255,0.1)',
                },
                '&.Mui-active': {
                  boxShadow: '0 0 0 12px rgba(255,255,255,0.1)',
                },
              },
            }}
          />
        </Box>

        {/* Total duration */}
        <Typography
          sx={{
            fontFamily: '"Inter", sans-serif',
            fontWeight: 500,
            fontSize: '14px',
            color: 'rgba(255,255,255,0.5)',
            minWidth: '36px',
            flexShrink: 0,
            fontVariantNumeric: 'tabular-nums',
          }}
        >
          {formatTime(duration)}
        </Typography>

        {/* Volume */}
        <Box
          sx={{
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
            flexShrink: 0,
          }}
        >
          {volume > 0.5 ? (
            <VolumeUpIcon sx={{ fontSize: 20, color: 'rgba(255,255,255,0.5)' }} />
          ) : (
            <VolumeDownIcon sx={{ fontSize: 20, color: 'rgba(255,255,255,0.5)' }} />
          )}
          <Slider
            value={volume}
            min={0}
            max={1}
            step={0.01}
            onChange={handleVolumeChange}
            sx={{
              width: 100,
              color: 'transparent',
              height: 4,
              padding: '12px 0',
              '& .MuiSlider-rail': {
                backgroundColor: 'rgba(255,255,255,0.12)',
                height: 4,
                borderRadius: 2,
              },
              '& .MuiSlider-track': {
                background: 'rgba(255,255,255,0.5)',
                height: 4,
                border: 'none',
                borderRadius: 2,
              },
              '& .MuiSlider-thumb': {
                width: 12,
                height: 12,
                backgroundColor: '#FFFFFF',
                '&:hover, &.Mui-focusVisible': {
                  boxShadow: '0 0 0 6px rgba(255,255,255,0.1)',
                },
              },
            }}
          />
        </Box>
      </Box>
    </Box>
  );
};
