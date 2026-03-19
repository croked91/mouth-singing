import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Box,
  Button,
  Chip,
  CircularProgress,
  Typography,
} from '@mui/material';
import MicIcon from '@mui/icons-material/Mic';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';
import QueueMusicIcon from '@mui/icons-material/QueueMusic';
import CloudUploadIcon from '@mui/icons-material/CloudUpload';
import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome';
import LockIcon from '@mui/icons-material/Lock';
import { CosmicBackground } from '../../components/CosmicBackground';
import { useSessionStore } from '../../store/sessionStore';

const FEATURE_PILLS = [
  { label: 'Очередь песен', icon: <QueueMusicIcon sx={{ fontSize: 16 }} /> },
  { label: 'Загрузите свой трек', icon: <CloudUploadIcon sx={{ fontSize: 16 }} /> },
  { label: 'ИИ-рекомендации', icon: <AutoAwesomeIcon sx={{ fontSize: 16 }} /> },
];

export const WelcomePage: React.FC = () => {
  const navigate = useNavigate();
  const createSession = useSessionStore((s) => s.createSession);
  const addParticipant = useSessionStore((s) => s.addParticipant);
  const [isStarting, setIsStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);

  const handleStart = async (): Promise<void> => {
    setIsStarting(true);
    setStartError(null);
    try {
      const sessionId = await createSession('default');
      await addParticipant('Певец');
      navigate(`/session/${sessionId}/queue`);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Ошибка создания сессии';
      setStartError(message);
      setIsStarting(false);
    }
  };

  return (
    <CosmicBackground>
      <Box
        sx={{
          minHeight: '100vh',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          px: 3,
          py: 6,
          position: 'relative',
        }}
      >
        {/* Logo */}
        <Box
          sx={{
            display: 'flex',
            alignItems: 'center',
            gap: 1.5,
            mb: 5,
          }}
        >
          <Box
            sx={{
              width: 56,
              height: 56,
              borderRadius: '16px',
              background: 'linear-gradient(135deg, #7C3AED 0%, #2563EB 100%)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              boxShadow: '0 0 24px rgba(124,58,237,0.5)',
            }}
          >
            <MicIcon sx={{ color: '#fff', fontSize: 30 }} />
          </Box>
          <Typography
            variant="h4"
            sx={{
              fontWeight: 800,
              letterSpacing: '0.18em',
              background: 'linear-gradient(135deg, #A78BFA 0%, #60A5FA 100%)',
              WebkitBackgroundClip: 'text',
              WebkitTextFillColor: 'transparent',
              backgroundClip: 'text',
            }}
          >
            KARAOKE
          </Typography>
        </Box>

        {/* Headline */}
        <Typography
          variant="h1"
          align="center"
          sx={{
            fontSize: { xs: '2.5rem', md: '3.5rem' },
            fontWeight: 800,
            color: '#FFFFFF',
            mb: 2.5,
            maxWidth: 640,
            textShadow: '0 0 40px rgba(124,58,237,0.55), 0 0 80px rgba(124,58,237,0.2)',
          }}
        >
          Пойте вместе сегодня
        </Typography>

        {/* Subtitle */}
        <Typography
          variant="body1"
          align="center"
          sx={{
            fontSize: '1.25rem',
            color: 'rgba(255,255,255,0.65)',
            mb: 5,
            maxWidth: 480,
            lineHeight: 1.6,
          }}
        >
          Выбирайте песни, вставайте в очередь и пусть ночь начнётся.
        </Typography>

        {/* CTA Button */}
        <Button
          variant="contained"
          size="large"
          startIcon={
            isStarting ? (
              <CircularProgress size={18} sx={{ color: '#fff' }} />
            ) : (
              <PlayArrowIcon />
            )
          }
          disabled={isStarting}
          onClick={handleStart}
          sx={{
            px: 5,
            py: 1.75,
            fontSize: '1.1rem',
            letterSpacing: '0.1em',
            mb: startError ? 1.5 : 4,
            boxShadow: '0 0 32px rgba(124,58,237,0.5), 0 4px 20px rgba(37,99,235,0.3)',
            '&:hover': {
              boxShadow: '0 0 48px rgba(124,58,237,0.7), 0 6px 28px rgba(37,99,235,0.4)',
            },
          }}
        >
          НАЧАТЬ СЕССИЮ
        </Button>

        {/* Error message */}
        {startError && (
          <Typography
            variant="body2"
            align="center"
            sx={{ color: 'error.main', mb: 3, maxWidth: 360 }}
          >
            {startError}
          </Typography>
        )}

        {/* Feature pills */}
        <Box
          sx={{
            display: 'flex',
            gap: 1.5,
            flexWrap: 'wrap',
            justifyContent: 'center',
          }}
        >
          {FEATURE_PILLS.map((pill) => (
            <Chip
              key={pill.label}
              label={pill.label}
              icon={pill.icon}
              sx={{
                fontSize: '0.875rem',
                fontWeight: 500,
                py: 2.5,
                px: 0.5,
                '& .MuiChip-icon': {
                  color: '#A78BFA',
                },
              }}
            />
          ))}
        </Box>

        {/* Admin link */}
        <Box
          component="a"
          href="/admin"
          onClick={(e: React.MouseEvent) => {
            e.preventDefault();
            navigate('/admin');
          }}
          sx={{
            position: 'fixed',
            bottom: 24,
            right: 24,
            display: 'flex',
            alignItems: 'center',
            gap: 0.75,
            color: 'rgba(255,255,255,0.30)',
            textDecoration: 'none',
            fontSize: '0.8125rem',
            fontWeight: 500,
            letterSpacing: '0.05em',
            transition: 'color 0.2s ease',
            '&:hover': {
              color: 'rgba(255,255,255,0.65)',
            },
          }}
        >
          <LockIcon sx={{ fontSize: 14 }} />
          Админ
        </Box>
      </Box>
    </CosmicBackground>
  );
};
