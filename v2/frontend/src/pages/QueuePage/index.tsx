import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  AppBar,
  Box,
  CircularProgress,
  IconButton,
  Tab,
  Tabs,
  Toolbar,
  Tooltip,
  Typography,
  Alert,
  Snackbar,
} from '@mui/material';
import MicIcon from '@mui/icons-material/Mic';
import LockIcon from '@mui/icons-material/Lock';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';
import QueueMusicIcon from '@mui/icons-material/QueueMusic';
import SearchIcon from '@mui/icons-material/Search';
import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome';
import CloudUploadIcon from '@mui/icons-material/CloudUpload';

import { CosmicBackground } from '../../components/CosmicBackground';
import { TrackCard } from '../../components/TrackCard';
import { QueueItem } from '../../components/QueueItem';
import { ParticipantSelector } from '../../components/ParticipantSelector';
import { SearchTab } from '../../components/SearchTab';
import { UploadTab } from '../../components/UploadTab';
import { useSessionStore } from '../../store/sessionStore';
import { useQueueStore } from '../../store/queueStore';
import { api } from '../../services/api';
import type { RecommendationResponse } from '../../types';

// ─── Constants ────────────────────────────────────────────────────────────────

const AVATAR_GRADIENTS = [
  'linear-gradient(135deg, #7C3AED, #EC4899)',
  'linear-gradient(135deg, #2563EB, #06B6D4)',
  'linear-gradient(135deg, #059669, #10B981)',
  'linear-gradient(135deg, #D97706, #F59E0B)',
];

const QUEUE_POLL_INTERVAL_MS = 5000;

const STRATEGY_LABELS: Record<string, string> = {
  popular: 'ПОПУЛЯРНОЕ',
  last: 'ПОХОЖЕЕ НА ПОСЛЕДНИЙ ТРЕК',
  last_two_avg: 'В ВАШЕМ СТИЛЕ',
  session_avg: 'НА ОСНОВЕ ВАШЕЙ СЕССИИ',
};

const TAB_RECOMMENDATIONS = 1;

// ─── Helpers ──────────────────────────────────────────────────────────────────

function getAvatarGradient(index: number): string {
  return AVATAR_GRADIENTS[index % AVATAR_GRADIENTS.length];
}

function getInitials(name: string): string {
  const words = name.trim().split(/\s+/);
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}

// ─── Component ────────────────────────────────────────────────────────────────

export const QueuePage: React.FC = () => {
  const { id: sessionId } = useParams<{ id: string }>();
  const navigate = useNavigate();

  // Store state
  const { participants, loadSession } = useSessionStore();
  const { currentEntry, upcoming, loadQueue, addToQueue } = useQueueStore();

  // Local state
  const [activeTab, setActiveTab] = useState<number>(TAB_RECOMMENDATIONS);
  const [selectedParticipantId, setSelectedParticipantId] = useState<string | null>(null);
  const [recommendations, setRecommendations] = useState<RecommendationResponse | null>(null);
  const [recsLoading, setRecsLoading] = useState(false);
  const [recsError, setRecsError] = useState<string | null>(null);
  const [addingTrackId, setAddingTrackId] = useState<string | null>(null);
  const [snackMessage, setSnackMessage] = useState<string | null>(null);

  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // ── Initial load ────────────────────────────────────────────────────────────

  useEffect(() => {
    if (!sessionId) return;

    void loadSession(sessionId);
    void loadQueue(sessionId);

    // Poll queue every 5s
    pollTimerRef.current = setInterval(() => {
      void loadQueue(sessionId);
    }, QUEUE_POLL_INTERVAL_MS);

    return () => {
      if (pollTimerRef.current !== null) {
        clearInterval(pollTimerRef.current);
      }
    };
  }, [sessionId, loadSession, loadQueue]);

  // ── Auto-select first participant ───────────────────────────────────────────

  useEffect(() => {
    if (participants.length > 0 && selectedParticipantId === null) {
      setSelectedParticipantId(participants[0].id);
    }
  }, [participants, selectedParticipantId]);

  // ── Fetch recommendations when participant or tab changes ───────────────────

  const fetchRecommendations = useCallback(
    async (participantId: string): Promise<void> => {
      if (!sessionId) return;

      setRecsLoading(true);
      setRecsError(null);

      try {
        const data = await api.getRecommendations(participantId, sessionId, 12);
        setRecommendations(data);
      } catch (err) {
        const message = err instanceof Error ? err.message : 'Ошибка загрузки рекомендаций';
        setRecsError(message);
      } finally {
        setRecsLoading(false);
      }
    },
    [sessionId]
  );

  useEffect(() => {
    if (activeTab === TAB_RECOMMENDATIONS && selectedParticipantId) {
      void fetchRecommendations(selectedParticipantId);
    }
  }, [activeTab, selectedParticipantId, fetchRecommendations]);

  // ── Handlers ────────────────────────────────────────────────────────────────

  const handleParticipantSelect = useCallback((id: string): void => {
    if (id === selectedParticipantId) return;
    setSelectedParticipantId(id);
    setRecommendations(null);
  }, [selectedParticipantId]);

  const handleTrackSelect = useCallback(
    async (trackId: string): Promise<void> => {
      if (!sessionId || !selectedParticipantId) return;

      setAddingTrackId(trackId);
      try {
        await addToQueue(sessionId, selectedParticipantId, trackId);
        setSnackMessage('Трек добавлен в очередь!');
      } catch (err) {
        const message = err instanceof Error ? err.message : 'Ошибка добавления в очередь';
        setSnackMessage(`Ошибка: ${message}`);
      } finally {
        setAddingTrackId(null);
      }
    },
    [sessionId, selectedParticipantId, addToQueue]
  );

  const handleSkip = useCallback(async (): Promise<void> => {
    if (!currentEntry || !sessionId) return;
    try {
      await api.skipTurn(currentEntry.id);
      void loadQueue(sessionId);
    } catch {
      // Silently ignore — queue will be refreshed by poll
    }
  }, [currentEntry, sessionId, loadQueue]);

  const handleAdminClick = useCallback((): void => {
    navigate('/admin');
  }, [navigate]);

  // ── Derived values ──────────────────────────────────────────────────────────

  const currentSinger = currentEntry?.participant ?? null;
  const currentSingerName = currentSinger?.display_name ?? null;
  const currentSingerIndex = currentSinger
    ? participants.findIndex((p) => p.id === currentSinger.id)
    : 0;

  // ── Render helpers ──────────────────────────────────────────────────────────

  const renderCurrentSingerCard = (): React.ReactNode => {
    if (!currentSingerName) {
      return (
        <Box
          sx={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            py: 5,
            gap: 1.5,
          }}
        >
          <QueueMusicIcon sx={{ fontSize: 40, color: 'rgba(124,58,237,0.4)' }} />
          <Typography
            sx={{
              fontSize: '14px',
              color: 'rgba(255,255,255,0.35)',
              textAlign: 'center',
            }}
          >
            Никто ещё не поёт
          </Typography>
        </Box>
      );
    }

    const gradient = getAvatarGradient(currentSingerIndex >= 0 ? currentSingerIndex : 0);

    return (
      <Box
        sx={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          gap: 2,
          py: 2,
        }}
      >
        {/* Avatar with animated glow ring */}
        <Box sx={{ position: 'relative' }}>
          {/* Outer glow ring */}
          <Box
            sx={{
              position: 'absolute',
              inset: -6,
              borderRadius: '50%',
              background: gradient,
              opacity: 0.35,
              filter: 'blur(8px)',
              '@keyframes glowPulse': {
                '0%, 100%': { opacity: 0.35, transform: 'scale(1)' },
                '50%': { opacity: 0.55, transform: 'scale(1.06)' },
              },
              animation: 'glowPulse 2.8s ease-in-out infinite',
            }}
          />

          {/* Avatar circle */}
          <Box
            sx={{
              width: 88,
              height: 88,
              borderRadius: '50%',
              background: gradient,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: '1.875rem',
              fontWeight: 700,
              color: '#fff',
              position: 'relative',
              zIndex: 1,
              boxShadow: '0 4px 24px rgba(0,0,0,0.4)',
            }}
          >
            {getInitials(currentSingerName)}
          </Box>

          {/* Status dot */}
          <Box
            sx={{
              position: 'absolute',
              bottom: 4,
              right: 4,
              width: 14,
              height: 14,
              borderRadius: '50%',
              backgroundColor: '#10B981',
              border: '2px solid #0D0B2B',
              zIndex: 2,
              '@keyframes statusPulse': {
                '0%, 100%': { boxShadow: '0 0 0 0 rgba(16,185,129,0.5)' },
                '50%': { boxShadow: '0 0 0 5px rgba(16,185,129,0)' },
              },
              animation: 'statusPulse 2s ease-in-out infinite',
            }}
          />
        </Box>

        {/* Singer name */}
        <Box sx={{ textAlign: 'center' }}>
          <Typography
            sx={{
              fontSize: '28px',
              fontWeight: 700,
              color: '#FFFFFF',
              lineHeight: 1.2,
              mb: 0.5,
            }}
          >
            {currentSingerName}
          </Typography>
          <Typography
            sx={{
              fontSize: '14px',
              color: '#A78BFA',
              fontWeight: 500,
            }}
          >
            Ваша очередь выбирать!
          </Typography>
        </Box>

        {/* Track info */}
        {currentEntry?.track && (
          <Box
            sx={{
              px: 2,
              py: 0.875,
              borderRadius: '12px',
              background: 'rgba(124,58,237,0.15)',
              border: '1px solid rgba(167,139,250,0.2)',
              textAlign: 'center',
              maxWidth: '90%',
            }}
          >
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
              {currentEntry.track.title}
            </Typography>
            <Typography
              sx={{
                fontSize: '12px',
                color: 'rgba(255,255,255,0.45)',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {currentEntry.track.artist}
            </Typography>
          </Box>
        )}

        {/* Processing indicator */}
        {currentEntry && currentEntry.track && currentEntry.track.status !== 'ready' && (
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
            <CircularProgress size={20} sx={{ color: '#A78BFA' }} />
            <Typography sx={{ fontSize: '13px', color: 'rgba(255,255,255,0.5)' }}>
              Трек обрабатывается...
            </Typography>
          </Box>
        )}

        {/* Play button */}
        {currentEntry && currentEntry.track?.status === 'ready' && (
          <Box
            component="button"
            onClick={() => {
              navigate(`/session/${sessionId}/play/${currentEntry.id}`);
            }}
            sx={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: 1,
              px: 4,
              py: 1.5,
              borderRadius: '16px',
              background: 'linear-gradient(135deg, #7C3AED, #2563EB)',
              border: 'none',
              color: '#FFFFFF',
              fontSize: '16px',
              fontWeight: 700,
              letterSpacing: '0.1em',
              textTransform: 'uppercase',
              cursor: 'pointer',
              boxShadow: '0 4px 24px rgba(124,58,237,0.4)',
              transition: 'all 0.2s ease',
              '&:hover': {
                transform: 'scale(1.04)',
                boxShadow: '0 6px 32px rgba(124,58,237,0.55)',
              },
            }}
          >
            <PlayArrowIcon sx={{ fontSize: 24 }} />
            ПЕТЬ
          </Box>
        )}
      </Box>
    );
  };

  const renderQueueStrip = (): React.ReactNode => (
    <Box>
      <Typography
        sx={{
          fontSize: '11px',
          fontWeight: 700,
          letterSpacing: '0.12em',
          color: 'rgba(255,255,255,0.35)',
          textTransform: 'uppercase',
          mb: 1.5,
        }}
      >
        СЛЕДУЮЩИЙ
      </Typography>

      {upcoming.length === 0 ? (
        <Box
          sx={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            py: 3,
            gap: 1,
            border: '1px dashed rgba(255,255,255,0.10)',
            borderRadius: '14px',
          }}
        >
          <QueueMusicIcon sx={{ fontSize: 28, color: 'rgba(124,58,237,0.4)' }} />
          <Typography
            sx={{
              fontSize: '12px',
              color: 'rgba(255,255,255,0.3)',
              textAlign: 'center',
            }}
          >
            Очередь пуста — выберите трек справа
          </Typography>
        </Box>
      ) : (
        <Box
          sx={{
            display: 'flex',
            gap: 2,
            overflowX: 'auto',
            pb: 1,
            // Hide scrollbar cross-browser
            scrollbarWidth: 'none',
            '&::-webkit-scrollbar': { display: 'none' },
          }}
        >
          {upcoming.map((entry, index) => (
            <QueueItem key={entry.id} entry={entry} index={index} />
          ))}
        </Box>
      )}
    </Box>
  );

  const renderRecommendationsTab = (): React.ReactNode => {
    if (participants.length === 0) {
      return (
        <Box
          sx={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            py: 8,
            gap: 2,
          }}
        >
          <Typography sx={{ color: 'rgba(255,255,255,0.35)', textAlign: 'center' }}>
            Нет участников в сессии
          </Typography>
        </Box>
      );
    }

    return (
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
        {/* Participant selector */}
        <ParticipantSelector
          participants={participants}
          selectedId={selectedParticipantId}
          onSelect={handleParticipantSelect}
        />

        {/* Strategy label */}
        {recommendations && (
          <Typography
            sx={{
              fontSize: '11px',
              fontWeight: 700,
              letterSpacing: '0.12em',
              color: 'rgba(255,255,255,0.35)',
              textTransform: 'uppercase',
            }}
          >
            {STRATEGY_LABELS[recommendations.strategy] ?? recommendations.strategy}
          </Typography>
        )}

        {/* Loading state */}
        {recsLoading && (
          <Box
            sx={{
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              gap: 2,
              py: 6,
            }}
          >
            <CircularProgress size={36} sx={{ color: '#7C3AED' }} />
            <Typography sx={{ fontSize: '13px', color: 'rgba(255,255,255,0.4)' }}>
              Подбираем треки...
            </Typography>
          </Box>
        )}

        {/* Error state */}
        {recsError && !recsLoading && (
          <Alert
            severity="error"
            sx={{
              backgroundColor: 'rgba(248,113,113,0.1)',
              border: '1px solid rgba(248,113,113,0.3)',
              color: '#FCA5A5',
              borderRadius: '12px',
              '& .MuiAlert-icon': { color: '#F87171' },
            }}
          >
            {recsError}
          </Alert>
        )}

        {/* No participant selected */}
        {!selectedParticipantId && !recsLoading && (
          <Typography
            sx={{
              fontSize: '14px',
              color: 'rgba(255,255,255,0.35)',
              textAlign: 'center',
              py: 4,
            }}
          >
            Выберите участника, чтобы увидеть рекомендации
          </Typography>
        )}

        {/* Track grid */}
        {recommendations && !recsLoading && recommendations.tracks.length > 0 && (
          <Box
            sx={{
              display: 'grid',
              gridTemplateColumns: '1fr 1fr',
              gap: 1.5,
            }}
          >
            {recommendations.tracks.map((track) => (
              <TrackCard
                key={track.id}
                track={track}
                onSelect={(trackId) => { void handleTrackSelect(trackId); }}
                isAdding={addingTrackId === track.id}
              />
            ))}
          </Box>
        )}

        {/* Empty recommendations */}
        {recommendations && !recsLoading && recommendations.tracks.length === 0 && (
          <Box
            sx={{
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              py: 6,
              gap: 1.5,
            }}
          >
            <AutoAwesomeIcon sx={{ fontSize: 36, color: 'rgba(124,58,237,0.4)' }} />
            <Typography sx={{ color: 'rgba(255,255,255,0.35)', textAlign: 'center' }}>
              Рекомендаций пока нет — сыграйте несколько треков
            </Typography>
          </Box>
        )}
      </Box>
    );
  };

  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <CosmicBackground>
      <Box
        sx={{
          display: 'flex',
          flexDirection: 'column',
          minHeight: '100vh',
        }}
      >
        {/* ── Top Navigation Bar ── */}
        <AppBar position="sticky" elevation={0} sx={{ height: 72 }}>
          <Toolbar
            sx={{
              height: 72,
              minHeight: '72px !important',
              justifyContent: 'space-between',
              px: { xs: 2, md: 3 },
            }}
          >
            {/* Left: Logo */}
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, minWidth: 140 }}>
              <Box
                sx={{
                  width: 36,
                  height: 36,
                  borderRadius: '10px',
                  background: 'linear-gradient(135deg, #7C3AED, #2563EB)',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  boxShadow: '0 0 12px rgba(124,58,237,0.4)',
                }}
              >
                <MicIcon sx={{ color: '#fff', fontSize: 20 }} />
              </Box>
              <Typography
                variant="h6"
                sx={{
                  fontWeight: 800,
                  letterSpacing: '0.15em',
                  background: 'linear-gradient(135deg, #A78BFA, #60A5FA)',
                  WebkitBackgroundClip: 'text',
                  WebkitTextFillColor: 'transparent',
                  backgroundClip: 'text',
                }}
              >
                KARAOKE
              </Typography>
            </Box>

            {/* Center: Now singing */}
            <Box
              sx={{
                display: 'flex',
                alignItems: 'center',
                gap: 1.25,
                position: 'absolute',
                left: '50%',
                transform: 'translateX(-50%)',
              }}
            >
              <Box
                sx={{
                  '@keyframes micPulse': {
                    '0%, 100%': { opacity: 1, transform: 'scale(1)' },
                    '50%': { opacity: 0.5, transform: 'scale(0.9)' },
                  },
                  animation: currentSingerName
                    ? 'micPulse 1.4s ease-in-out infinite'
                    : 'none',
                  display: 'flex',
                  alignItems: 'center',
                }}
              >
                <MicIcon
                  sx={{
                    fontSize: 18,
                    color: currentSingerName ? '#A78BFA' : 'rgba(255,255,255,0.25)',
                  }}
                />
              </Box>

              <Typography
                sx={{
                  fontSize: '13px',
                  fontWeight: 700,
                  letterSpacing: '0.08em',
                  textTransform: 'uppercase',
                  color: currentSingerName
                    ? '#FFFFFF'
                    : 'rgba(255,255,255,0.35)',
                }}
              >
                {currentSingerName
                  ? `СЕЙЧАС ПОЁТ: ${currentSingerName}`
                  : 'НИКТО НЕ ПОЁТ'}
              </Typography>
            </Box>

            {/* Right: Skip + Admin */}
            <Box
              sx={{
                display: 'flex',
                alignItems: 'center',
                gap: 1,
                minWidth: 140,
                justifyContent: 'flex-end',
              }}
            >
              {currentEntry && (
                <Box
                  component="button"
                  onClick={() => { void handleSkip(); }}
                  sx={{
                    px: 2,
                    py: 0.625,
                    borderRadius: '20px',
                    background: 'rgba(248,113,113,0.15)',
                    border: '1px solid rgba(248,113,113,0.35)',
                    color: '#FCA5A5',
                    fontSize: '12px',
                    fontWeight: 700,
                    letterSpacing: '0.08em',
                    textTransform: 'uppercase',
                    cursor: 'pointer',
                    transition: 'all 0.2s ease',
                    '&:hover': {
                      background: 'rgba(248,113,113,0.25)',
                      borderColor: 'rgba(248,113,113,0.55)',
                    },
                  }}
                >
                  ПРОПУСТИТЬ
                </Box>
              )}

              <Tooltip title="Панель администратора" placement="bottom">
                <IconButton
                  size="small"
                  onClick={handleAdminClick}
                  sx={{ color: 'rgba(255,255,255,0.4)' }}
                >
                  <LockIcon sx={{ fontSize: 18 }} />
                </IconButton>
              </Tooltip>
            </Box>
          </Toolbar>
        </AppBar>

        {/* ── Two-panel layout ── */}
        <Box
          sx={{
            flex: 1,
            display: 'flex',
            gap: 0,
            overflow: 'hidden',
          }}
        >
          {/* ── Left Panel (480px fixed) ── */}
          <Box
            sx={{
              width: 480,
              flexShrink: 0,
              display: 'flex',
              flexDirection: 'column',
              borderRight: '1px solid rgba(255,255,255,0.08)',
              overflowY: 'auto',
              p: 3,
              gap: 3,
            }}
          >
            {/* Current Singer Card (glassmorphism) */}
            <Box
              sx={{
                background: 'rgba(255,255,255,0.06)',
                border: '1px solid rgba(255,255,255,0.12)',
                backdropFilter: 'blur(24px)',
                borderRadius: '24px',
                p: 3,
              }}
            >
              {renderCurrentSingerCard()}
            </Box>

            {/* Queue Strip */}
            <Box
              sx={{
                background: 'rgba(255,255,255,0.04)',
                border: '1px solid rgba(255,255,255,0.08)',
                borderRadius: '20px',
                p: 2.5,
              }}
            >
              {renderQueueStrip()}
            </Box>
          </Box>

          {/* ── Right Panel (flex: 1) ── */}
          <Box
            sx={{
              flex: 1,
              display: 'flex',
              flexDirection: 'column',
              overflowY: 'auto',
              p: 3,
            }}
          >
            {/* Tab bar */}
            <Box
              sx={{
                background: 'rgba(255,255,255,0.04)',
                border: '1px solid rgba(255,255,255,0.08)',
                borderRadius: '16px',
                p: '4px',
                mb: 3,
              }}
            >
              <Tabs
                value={activeTab}
                onChange={(_, newValue: number) => setActiveTab(newValue)}
                variant="fullWidth"
                TabIndicatorProps={{ style: { display: 'none' } }}
                sx={{
                  minHeight: 'unset',
                  '& .MuiTab-root': {
                    minHeight: 40,
                    py: 0.875,
                    px: 1.5,
                    borderRadius: '12px',
                    fontSize: '13px',
                    fontWeight: 600,
                    letterSpacing: '0.04em',
                    textTransform: 'none',
                    color: 'rgba(255,255,255,0.5)',
                    transition: 'all 0.2s ease',
                    gap: 0.75,
                    '&.Mui-selected': {
                      color: '#A78BFA',
                      background: 'rgba(124,58,237,0.35)',
                      border: '1px solid rgba(167,139,250,0.4)',
                    },
                    '& .MuiTab-iconWrapper': {
                      mb: '0 !important',
                    },
                  },
                }}
              >
                <Tab
                  icon={<SearchIcon sx={{ fontSize: 16 }} />}
                  iconPosition="start"
                  label="Поиск трека"
                />
                <Tab
                  icon={<AutoAwesomeIcon sx={{ fontSize: 16 }} />}
                  iconPosition="start"
                  label="Рекомендации"
                />
                <Tab
                  icon={<CloudUploadIcon sx={{ fontSize: 16 }} />}
                  iconPosition="start"
                  label="Загрузить"
                />
              </Tabs>
            </Box>

            {/* Tab content */}
            <Box sx={{ flex: 1 }}>
              {activeTab === 0 && sessionId && (
                <SearchTab
                  sessionId={sessionId}
                  selectedParticipantId={selectedParticipantId}
                  onTrackSelected={(trackId) => { void handleTrackSelect(trackId); }}
                />
              )}
              {activeTab === TAB_RECOMMENDATIONS && renderRecommendationsTab()}
              {activeTab === 2 && sessionId && (
                <UploadTab
                  sessionId={sessionId}
                  selectedParticipantId={selectedParticipantId}
                  onTrackUploaded={(trackId) => { void handleTrackSelect(trackId); }}
                />
              )}
            </Box>
          </Box>
        </Box>
      </Box>

      {/* Success / error snackbar */}
      <Snackbar
        open={snackMessage !== null}
        autoHideDuration={3000}
        onClose={() => setSnackMessage(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
        message={snackMessage}
        ContentProps={{
          sx: {
            background: snackMessage?.startsWith('Ошибка')
              ? 'rgba(220,38,38,0.9)'
              : 'rgba(16,185,129,0.9)',
            backdropFilter: 'blur(12px)',
            border: '1px solid rgba(255,255,255,0.15)',
            borderRadius: '12px',
            fontSize: '14px',
            fontWeight: 500,
          },
        }}
      />
    </CosmicBackground>
  );
};
