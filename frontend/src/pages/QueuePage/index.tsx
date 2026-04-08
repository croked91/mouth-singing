import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  AppBar,
  Box,
  Chip,
  CircularProgress,
  IconButton,
  Snackbar,
  Toolbar,
  Tooltip,
  Typography,
  Alert,
} from '@mui/material';
import MicIcon from '@mui/icons-material/Mic';
import LockIcon from '@mui/icons-material/Lock';
import HistoryIcon from '@mui/icons-material/History';
import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome';

import { CosmicBackground } from '../../components/CosmicBackground';
import { TrackCard } from '../../components/TrackCard';
import { SearchTab } from '../../components/SearchTab';
import { UploadTab } from '../../components/UploadTab';
import { HistoryDrawer } from '../../components/HistoryDrawer';
import { useSessionStore } from '../../store/sessionStore';
import { api } from '../../services/api';
import type { RecommendationResponse, MoodTag } from '../../types';

// ─── Constants ────────────────────────────────────────────────────────────────

const STRATEGY_LABELS: Record<string, string> = {
  popular: 'ПОПУЛЯРНОЕ',
  cluster: 'НАСТРОЕНИЕ',
};

// ─── Component ────────────────────────────────────────────────────────────────

export const QueuePage: React.FC = () => {
  const { id: sessionId } = useParams<{ id: string }>();
  const navigate = useNavigate();

  // Session store
  const { participants, loadSession } = useSessionStore();

  // Recommendations state
  const [recommendations, setRecommendations] = useState<RecommendationResponse | null>(null);
  const [recsLoading, setRecsLoading] = useState(false);
  const [recsError, setRecsError] = useState<string | null>(null);

  // Mood tags
  const [moodTags, setMoodTags] = useState<MoodTag[]>([]);
  const [selectedTagId, setSelectedTagId] = useState<number | null>(null);
  const [russianOnly, setRussianOnly] = useState(false);
  const [recsRefreshCounter, setRecsRefreshCounter] = useState(0);

  // Play action
  const [playingTrackId, setPlayingTrackId] = useState<string | null>(null);
  const [snackMessage, setSnackMessage] = useState<string | null>(null);

  // History drawer
  const [historyOpen, setHistoryOpen] = useState(false);

  // Search active — when SearchTab has results, hide recommendations
  const [searchActive, setSearchActive] = useState(false);

  // Track shown counts for recommendation rotation
  const getShownCounts = (): Record<string, number> => {
    try {
      const raw = sessionStorage.getItem(`shownCounts_${sessionId}`);
      return raw ? JSON.parse(raw) : {};
    } catch {
      return {};
    }
  };
  const setShownCounts = (counts: Record<string, number>) => {
    try {
      sessionStorage.setItem(`shownCounts_${sessionId}`, JSON.stringify(counts));
    } catch {
      // sessionStorage unavailable
    }
  };
  const shownCountRef = useRef<Record<string, number>>(getShownCounts());

  // ── Initial load ────────────────────────────────────────────────────────────

  useEffect(() => {
    if (!sessionId) return;
    void loadSession(sessionId);
    void api.getTags(sessionId).then(setMoodTags).catch(() => {});
  }, [sessionId, loadSession]);

  // ── Fetch recommendations ─────────────────────────────────────────────────

  const fetchRecommendations = useCallback(
    async (tagId?: number): Promise<void> => {
      if (!sessionId) return;
      setRecsLoading(true);
      setRecsError(null);
      try {
        const language = russianOnly ? 'ru' : undefined;
        let excludeIds: string[] | undefined;
        if (tagId === undefined) {
          const dismissed = Object.entries(shownCountRef.current)
            .filter(([, count]) => count >= 2)
            .map(([id]) => id);
          if (dismissed.length > 0) excludeIds = dismissed;
        }
        const data = await api.getRecommendations(sessionId, 10, tagId, language, excludeIds);
        if (tagId === undefined) {
          for (const t of data.tracks) {
            shownCountRef.current[t.id] = (shownCountRef.current[t.id] || 0) + 1;
          }
          setShownCounts(shownCountRef.current);
        }
        setRecommendations(data);
      } catch (err) {
        setRecsError(err instanceof Error ? err.message : 'Ошибка загрузки рекомендаций');
      } finally {
        setRecsLoading(false);
      }
    },
    [sessionId, russianOnly],
  );

  useEffect(() => {
    void fetchRecommendations(selectedTagId ?? undefined);
    if (sessionId && recsRefreshCounter > 0) {
      void api.getTags(sessionId).then(setMoodTags).catch(() => {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedTagId, recsRefreshCounter, russianOnly]);

  // ── Handlers ────────────────────────────────────────────────────────────────

  const ensureParticipant = useCallback(async (): Promise<string> => {
    if (participants.length > 0) return participants[0].id;
    const { addParticipant } = useSessionStore.getState();
    const p = await addParticipant('Певец');
    return p.id;
  }, [participants]);

  const handlePlay = useCallback(
    async (trackId: string): Promise<void> => {
      if (!sessionId || playingTrackId) return;
      setPlayingTrackId(trackId);
      try {
        const participantId = await ensureParticipant();
        const startData = await api.directPlay(sessionId, participantId, trackId);
        // Reset shown count — track was selected.
        delete shownCountRef.current[trackId];
        setShownCounts(shownCountRef.current);
        setRecsRefreshCounter((c) => c + 1);
        navigate(`/session/${sessionId}/play/${startData.entry_id}`, {
          state: { startData },
        });
      } catch (err) {
        const message = err instanceof Error ? err.message : 'Ошибка воспроизведения';
        setSnackMessage(`Ошибка: ${message}`);
      } finally {
        setPlayingTrackId(null);
      }
    },
    [sessionId, playingTrackId, ensureParticipant, navigate],
  );

  const handleTagClick = useCallback((tagId: number): void => {
    setSelectedTagId((prev) => (prev === tagId ? null : tagId));
  }, []);

  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <CosmicBackground>
      <Box sx={{ display: 'flex', flexDirection: 'column', height: '100vh', overflow: 'hidden' }}>

        {/* ── AppBar ── */}
        <AppBar position="sticky" elevation={0} sx={{ height: 64 }}>
          <Toolbar
            sx={{
              height: 64,
              minHeight: '64px !important',
              justifyContent: 'space-between',
              px: { xs: 2, md: 3 },
            }}
          >
            {/* Logo */}
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
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

            {/* Right: History + Admin */}
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
              <Tooltip title="История сессии" placement="bottom">
                <IconButton
                  size="small"
                  onClick={() => setHistoryOpen(true)}
                  sx={{ color: 'rgba(255,255,255,0.5)' }}
                >
                  <HistoryIcon sx={{ fontSize: 20 }} />
                </IconButton>
              </Tooltip>
              <Tooltip title="Панель администратора" placement="bottom">
                <IconButton
                  size="small"
                  onClick={() => navigate('/admin')}
                  sx={{ color: 'rgba(255,255,255,0.4)' }}
                >
                  <LockIcon sx={{ fontSize: 18 }} />
                </IconButton>
              </Tooltip>
            </Box>
          </Toolbar>
        </AppBar>

        {/* ── Two-column layout ── */}
        <Box sx={{ flex: 1, display: 'flex', overflow: 'hidden' }}>

          {/* ── Left column: Search + Recommendations ── */}
          <Box
            sx={{
              flex: 1,
              display: 'flex',
              flexDirection: 'column',
              overflowY: 'auto',
              p: 3,
            }}
          >
            {/* Search (always visible) */}
            {sessionId && (
              <Box sx={{ mb: searchActive ? 0 : 3 }}>
                <SearchTab
                  sessionId={sessionId}
                  onTrackSelected={(trackId) => { void handlePlay(trackId); }}
                  onSearchStateChange={setSearchActive}
                />
              </Box>
            )}

            {/* Recommendations (hidden when search is active) */}
            {!searchActive && (
              <Box sx={{ display: 'flex', flexDirection: 'column', gap: 3 }}>

                {/* Mood tags + language filter */}
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap' }}>
                  {recommendations && (
                    <Typography
                      sx={{
                        fontSize: '11px',
                        fontWeight: 700,
                        letterSpacing: '0.12em',
                        color: 'rgba(255,255,255,0.35)',
                        textTransform: 'uppercase',
                        flexShrink: 0,
                      }}
                    >
                      {STRATEGY_LABELS[recommendations.strategy] ?? recommendations.strategy}
                    </Typography>
                  )}

                  {moodTags.slice(0, 5).map((tag) => (
                    <Chip
                      key={tag.id}
                      label={tag.name}
                      onClick={() => handleTagClick(tag.id)}
                      variant={selectedTagId === tag.id ? 'filled' : 'outlined'}
                      size="small"
                      sx={{
                        flexShrink: 0,
                        borderRadius: '20px',
                        fontSize: '12px',
                        fontWeight: 600,
                        transition: 'all 0.2s ease',
                        ...(selectedTagId === tag.id
                          ? {
                              background: 'linear-gradient(135deg, #7C3AED, #2563EB)',
                              color: '#FFFFFF',
                              border: '1px solid rgba(167,139,250,0.5)',
                              boxShadow: '0 2px 12px rgba(124,58,237,0.4)',
                            }
                          : {
                              background: 'rgba(255,255,255,0.06)',
                              color: 'rgba(255,255,255,0.6)',
                              borderColor: 'rgba(255,255,255,0.15)',
                              '&:hover': {
                                background: 'rgba(124,58,237,0.15)',
                                borderColor: 'rgba(167,139,250,0.4)',
                                color: '#A78BFA',
                              },
                            }),
                      }}
                    />
                  ))}

                  {/* Language toggle */}
                  <Box sx={{ display: 'flex', alignItems: 'center', ml: 'auto', flexShrink: 0 }}>
                    <Chip
                      label="RU"
                      onClick={() => setRussianOnly((prev) => !prev)}
                      variant={russianOnly ? 'filled' : 'outlined'}
                      size="small"
                      sx={{
                        borderRadius: '20px',
                        fontSize: '11px',
                        fontWeight: 700,
                        minWidth: 40,
                        ...(russianOnly
                          ? {
                              background: 'rgba(124,58,237,0.4)',
                              color: '#A78BFA',
                              border: '1px solid rgba(167,139,250,0.4)',
                            }
                          : {
                              background: 'rgba(255,255,255,0.06)',
                              color: 'rgba(255,255,255,0.4)',
                              borderColor: 'rgba(255,255,255,0.12)',
                            }),
                      }}
                    />
                  </Box>
                </Box>

                {/* Loading */}
                {recsLoading && (
                  <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 2, py: 6 }}>
                    <CircularProgress size={36} sx={{ color: '#7C3AED' }} />
                    <Typography sx={{ fontSize: '13px', color: 'rgba(255,255,255,0.4)' }}>
                      Подбираем треки...
                    </Typography>
                  </Box>
                )}

                {/* Error */}
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

                {/* Track list */}
                {recommendations && !recsLoading && recommendations.tracks.length > 0 && (
                  <Box sx={{ display: 'grid', gridTemplateColumns: '1fr', gap: 1.5 }}>
                    {recommendations.tracks.map((track) => (
                      <TrackCard
                        key={track.id}
                        track={track}
                        onSelect={(trackId) => { void handlePlay(trackId); }}
                        isAdding={playingTrackId === track.id}
                      />
                    ))}
                  </Box>
                )}

                {/* Empty */}
                {recommendations && !recsLoading && recommendations.tracks.length === 0 && (
                  <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', py: 6, gap: 1.5 }}>
                    <AutoAwesomeIcon sx={{ fontSize: 36, color: 'rgba(124,58,237,0.4)' }} />
                    <Typography sx={{ color: 'rgba(255,255,255,0.35)', textAlign: 'center' }}>
                      Пока нет рекомендаций — найдите трек через поиск или загрузите свой
                    </Typography>
                  </Box>
                )}
              </Box>
            )}
          </Box>

          {/* ── Right column: Upload ── */}
          <Box
            sx={{
              width: '25%',
              minWidth: 260,
              maxWidth: 400,
              flexShrink: 0,
              borderLeft: '1px solid rgba(255,255,255,0.08)',
              overflow: 'hidden',
              display: 'flex',
              flexDirection: 'column',
              p: 2,
            }}
          >
            {sessionId && (
              <UploadTab
                sessionId={sessionId}
                onPlay={(trackId) => { void handlePlay(trackId); }}
                compact
              />
            )}
          </Box>
        </Box>
      </Box>

      {/* History drawer */}
      {sessionId && (
        <HistoryDrawer
          open={historyOpen}
          onClose={() => setHistoryOpen(false)}
          sessionId={sessionId}
          onTrackSelect={(trackId) => {
            setHistoryOpen(false);
            void handlePlay(trackId);
          }}
        />
      )}

      {/* Snackbar */}
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
