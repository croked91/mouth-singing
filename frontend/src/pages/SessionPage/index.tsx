import React, { useState, useEffect, useCallback } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  AppBar,
  Box,
  Button,
  Chip,
  CircularProgress,
  Paper,
  TextField,
  Toolbar,
  Typography,
  Alert,
} from '@mui/material';
import MicIcon from '@mui/icons-material/Mic';
import AutoFixHighIcon from '@mui/icons-material/AutoFixHigh';
import AddIcon from '@mui/icons-material/Add';
import CloseIcon from '@mui/icons-material/Close';
import PersonIcon from '@mui/icons-material/Person';
import { CosmicBackground } from '../../components/CosmicBackground';
import { useSessionStore } from '../../store/sessionStore';
import type { Participant } from '../../types';

const AVATAR_GRADIENTS = [
  'linear-gradient(135deg, #7C3AED, #A78BFA)',
  'linear-gradient(135deg, #2563EB, #60A5FA)',
  'linear-gradient(135deg, #06B6D4, #67E8F9)',
  'linear-gradient(135deg, #10B981, #6EE7B7)',
];

function getAvatarGradient(index: number): string {
  return AVATAR_GRADIENTS[index % AVATAR_GRADIENTS.length];
}

function getInitials(name: string): string {
  const words = name.trim().split(/\s+/);
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}

export const SessionPage: React.FC = () => {
  const { id: sessionId } = useParams<{ id: string }>();
  const navigate = useNavigate();

  const {
    participants,
    isLoading,
    error,
    loadSession,
    addParticipant,
    removeParticipantLocally,
    clearError,
  } = useSessionStore();

  const [nicknameInput, setNicknameInput] = useState('');
  const [addingParticipant, setAddingParticipant] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  useEffect(() => {
    if (sessionId) {
      loadSession(sessionId).catch(() => {
        // Error is stored in the store
      });
    }
  }, [sessionId, loadSession]);

  const handleAddParticipant = useCallback(async (): Promise<void> => {
    const name = nicknameInput.trim();
    if (!name) return;

    setAddingParticipant(true);
    setLocalError(null);
    try {
      await addParticipant(name);
      setNicknameInput('');
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Ошибка добавления участника';
      setLocalError(message);
    } finally {
      setAddingParticipant(false);
    }
  }, [nicknameInput, addParticipant]);

  const handleGenerate = useCallback(async (): Promise<void> => {
    setAddingParticipant(true);
    setLocalError(null);
    try {
      await addParticipant();
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Ошибка генерации участника';
      setLocalError(message);
    } finally {
      setAddingParticipant(false);
    }
  }, [addParticipant]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent): void => {
      if (e.key === 'Enter') {
        void handleAddParticipant();
      }
    },
    [handleAddParticipant]
  );

  const handleStart = useCallback((): void => {
    if (sessionId) {
      navigate(`/session/${sessionId}/queue`);
    }
  }, [sessionId, navigate]);

  const handleRemove = useCallback(
    (participant: Participant): void => {
      removeParticipantLocally(participant.id);
    },
    [removeParticipantLocally]
  );

  const displayError = error || localError;

  return (
    <CosmicBackground>
      {/* Top navigation bar */}
      <AppBar position="sticky" elevation={0}>
        <Toolbar sx={{ justifyContent: 'space-between', py: 1 }}>
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

          <Box
            sx={{
              px: 2.5,
              py: 0.75,
              borderRadius: '20px',
              background: 'rgba(124,58,237,0.15)',
              border: '1px solid rgba(167,139,250,0.3)',
            }}
          >
            <Typography
              variant="caption"
              sx={{
                color: '#A78BFA',
                fontWeight: 700,
                letterSpacing: '0.1em',
                textTransform: 'uppercase',
              }}
            >
              НОВАЯ СЕССИЯ
            </Typography>
          </Box>

          <Box sx={{ width: 120 }} />
        </Toolbar>
      </AppBar>

      {/* Main content */}
      <Box
        sx={{
          display: 'flex',
          justifyContent: 'center',
          px: 3,
          py: 6,
        }}
      >
        {isLoading && participants.length === 0 ? (
          <Box
            sx={{
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              gap: 2,
              py: 8,
            }}
          >
            <CircularProgress size={48} />
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>
              Загрузка сессии...
            </Typography>
          </Box>
        ) : (
          <Paper
            elevation={2}
            sx={{
              width: '100%',
              maxWidth: 780,
              p: { xs: 3, md: 5 },
              background: 'rgba(255,255,255,0.06)',
              border: '1px solid rgba(255,255,255,0.12)',
              backdropFilter: 'blur(24px)',
              borderRadius: '24px',
            }}
          >
            {/* Header */}
            <Box sx={{ mb: 4 }}>
              <Typography
                variant="h3"
                sx={{
                  fontWeight: 700,
                  color: '#FFFFFF',
                  mb: 1,
                }}
              >
                Кто поёт сегодня?
              </Typography>
              <Typography
                variant="body1"
                sx={{ color: 'rgba(255,255,255,0.55)' }}
              >
                Добавьте всех, кто будет петь
              </Typography>
            </Box>

            {/* Error alert */}
            {displayError && (
              <Alert
                severity="error"
                onClose={displayError === error ? clearError : () => setLocalError(null)}
                sx={{
                  mb: 3,
                  backgroundColor: 'rgba(248,113,113,0.1)',
                  border: '1px solid rgba(248,113,113,0.3)',
                  color: '#FCA5A5',
                  '& .MuiAlert-icon': { color: '#F87171' },
                }}
              >
                {displayError}
              </Alert>
            )}

            {/* Participants list */}
            <Box sx={{ mb: 4 }}>
              {participants.length === 0 ? (
                <Box
                  sx={{
                    display: 'flex',
                    flexDirection: 'column',
                    alignItems: 'center',
                    justifyContent: 'center',
                    py: 5,
                    border: '2px dashed rgba(255,255,255,0.12)',
                    borderRadius: '16px',
                    gap: 1.5,
                  }}
                >
                  <PersonIcon
                    sx={{ fontSize: 40, color: 'rgba(255,255,255,0.2)' }}
                  />
                  <Typography
                    variant="body2"
                    align="center"
                    sx={{ color: 'rgba(255,255,255,0.35)', maxWidth: 280 }}
                  >
                    Участников пока нет — добавьте первого певца
                  </Typography>
                </Box>
              ) : (
                <Box
                  sx={{
                    display: 'flex',
                    flexWrap: 'wrap',
                    gap: 1.5,
                  }}
                >
                  {participants.map((participant, index) => (
                    <Chip
                      key={participant.id}
                      label={
                        <Box
                          sx={{ display: 'flex', alignItems: 'center', gap: 1 }}
                        >
                          <Box
                            sx={{
                              width: 24,
                              height: 24,
                              borderRadius: '50%',
                              background: getAvatarGradient(index),
                              display: 'flex',
                              alignItems: 'center',
                              justifyContent: 'center',
                              fontSize: '0.65rem',
                              fontWeight: 700,
                              color: '#fff',
                              flexShrink: 0,
                            }}
                          >
                            {getInitials(participant.display_name)}
                          </Box>
                          <Typography
                            variant="body2"
                            sx={{ fontWeight: 500, color: '#FFFFFF' }}
                          >
                            {participant.display_name}
                          </Typography>
                        </Box>
                      }
                      onDelete={() => handleRemove(participant)}
                      deleteIcon={<CloseIcon sx={{ fontSize: '14px !important' }} />}
                      sx={{
                        height: 40,
                        backgroundColor: 'rgba(255,255,255,0.07)',
                        border: `1px solid ${AVATAR_GRADIENTS[index % AVATAR_GRADIENTS.length].includes('#7C3AED') ? 'rgba(167,139,250,0.35)' : 'rgba(96,165,250,0.35)'}`,
                        '& .MuiChip-label': { px: 1 },
                        '& .MuiChip-deleteIcon': {
                          color: 'rgba(255,255,255,0.4)',
                          '&:hover': { color: '#F87171' },
                        },
                      }}
                    />
                  ))}
                </Box>
              )}
            </Box>

            {/* Input row */}
            <Box
              sx={{
                display: 'flex',
                gap: 1.5,
                mb: 2.5,
                flexWrap: 'wrap',
              }}
            >
              <TextField
                fullWidth
                placeholder="Введите никнейм..."
                value={nicknameInput}
                onChange={(e) => setNicknameInput(e.target.value)}
                onKeyDown={handleKeyDown}
                disabled={addingParticipant}
                sx={{ flex: 1, minWidth: 200 }}
                inputProps={{ maxLength: 50 }}
              />
              <Button
                variant="outlined"
                startIcon={
                  addingParticipant ? (
                    <CircularProgress size={16} sx={{ color: '#A78BFA' }} />
                  ) : (
                    <AutoFixHighIcon />
                  )
                }
                onClick={handleGenerate}
                disabled={addingParticipant}
                sx={{ whiteSpace: 'nowrap', flexShrink: 0 }}
              >
                Сгенерировать
              </Button>
            </Box>

            {/* Add button */}
            <Button
              variant="outlined"
              fullWidth
              startIcon={<AddIcon />}
              onClick={handleAddParticipant}
              disabled={addingParticipant || !nicknameInput.trim()}
              sx={{
                mb: 4,
                py: 1.25,
                fontSize: '0.9375rem',
              }}
            >
              + ДОБАВИТЬ
            </Button>

            {/* Divider */}
            <Box
              sx={{
                height: '1px',
                background: 'rgba(255,255,255,0.10)',
                mb: 4,
              }}
            />

            {/* Start button */}
            <Button
              variant="contained"
              fullWidth
              size="large"
              disabled={participants.length === 0 || isLoading}
              onClick={handleStart}
              sx={{
                py: 2,
                fontSize: '1.125rem',
                letterSpacing: '0.1em',
              }}
            >
              ПОЕХАЛИ!
            </Button>

            {participants.length === 0 && (
              <Typography
                variant="caption"
                align="center"
                display="block"
                sx={{ mt: 1.5, color: 'rgba(255,255,255,0.35)' }}
              >
                Добавьте хотя бы одного участника чтобы начать
              </Typography>
            )}
          </Paper>
        )}
      </Box>
    </CosmicBackground>
  );
};
