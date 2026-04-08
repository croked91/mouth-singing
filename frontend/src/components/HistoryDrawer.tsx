import React, { useEffect, useState } from 'react';
import {
  Box,
  Drawer,
  IconButton,
  Typography,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';
import HistoryIcon from '@mui/icons-material/History';
import MusicNoteIcon from '@mui/icons-material/MusicNote';

import { api } from '../services/api';
import type { HistoryItem } from '../types';

// ─── Helpers ──────────────────────────────────────────────────────────────────

function timeAgo(isoDate: string): string {
  const diff = Date.now() - new Date(isoDate).getTime();
  const minutes = Math.floor(diff / 60_000);
  if (minutes < 1) return 'только что';
  if (minutes < 60) return `${minutes} мин назад`;
  const hours = Math.floor(minutes / 60);
  return `${hours} ч назад`;
}

// ─── Props ────────────────────────────────────────────────────────────────────

interface HistoryDrawerProps {
  open: boolean;
  onClose: () => void;
  sessionId: string;
  onTrackSelect: (trackId: string) => void;
}

// ─── Component ────────────────────────────────────────────────────────────────

export const HistoryDrawer: React.FC<HistoryDrawerProps> = ({
  open,
  onClose,
  sessionId,
  onTrackSelect,
}) => {
  const [items, setItems] = useState<HistoryItem[]>([]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;

    const load = async (): Promise<void> => {
      try {
        const data = await api.getSessionHistory(sessionId);
        if (!cancelled) setItems(data);
      } catch {
        // ignore
      }
    };

    void load();
    return () => { cancelled = true; };
  }, [open, sessionId]);

  return (
    <Drawer
      anchor="right"
      open={open}
      onClose={onClose}
      PaperProps={{
        sx: {
          width: 360,
          background: 'rgba(13,11,43,0.97)',
          backdropFilter: 'blur(24px)',
          borderLeft: '1px solid rgba(255,255,255,0.08)',
        },
      }}
    >
      {/* Header */}
      <Box
        sx={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          px: 2.5,
          py: 2,
          borderBottom: '1px solid rgba(255,255,255,0.08)',
        }}
      >
        <Typography
          sx={{
            fontSize: '13px',
            fontWeight: 700,
            letterSpacing: '0.12em',
            color: 'rgba(255,255,255,0.5)',
            textTransform: 'uppercase',
          }}
        >
          ИСТОРИЯ СЕССИИ
        </Typography>
        <IconButton
          size="small"
          onClick={onClose}
          sx={{ color: 'rgba(255,255,255,0.4)' }}
        >
          <CloseIcon sx={{ fontSize: 18 }} />
        </IconButton>
      </Box>

      {/* Content */}
      <Box sx={{ flex: 1, overflowY: 'auto', p: 2 }}>
        {items.length === 0 && (
          <Box
            sx={{
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              py: 6,
              gap: 1.5,
            }}
          >
            <HistoryIcon sx={{ fontSize: 36, color: 'rgba(255,255,255,0.15)' }} />
            <Typography sx={{ fontSize: '13px', color: 'rgba(255,255,255,0.3)', textAlign: 'center' }}>
              Пока ничего не спето
            </Typography>
          </Box>
        )}

        {items.length > 0 && (
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
            {items.map((item, i) => (
              <Box
                key={`${item.track_id}-${i}`}
                sx={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 1.5,
                  px: 1.5,
                  py: 1,
                  borderRadius: '12px',
                  background: 'rgba(255,255,255,0.04)',
                  border: '1px solid rgba(255,255,255,0.06)',
                  transition: 'background 0.15s ease',
                  '&:hover': { background: 'rgba(255,255,255,0.07)' },
                }}
              >
                {/* Artist image */}
                {item.artist_image_url ? (
                  <Box
                    component="img"
                    src={item.artist_image_url}
                    alt={item.artist}
                    sx={{
                      width: 40,
                      height: 40,
                      borderRadius: '8px',
                      objectFit: 'cover',
                      flexShrink: 0,
                    }}
                  />
                ) : (
                  <Box
                    sx={{
                      width: 40,
                      height: 40,
                      borderRadius: '8px',
                      background: 'linear-gradient(135deg, rgba(124,58,237,0.4), rgba(37,99,235,0.4))',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      flexShrink: 0,
                    }}
                  >
                    <MusicNoteIcon sx={{ fontSize: 18, color: 'rgba(255,255,255,0.6)' }} />
                  </Box>
                )}

                {/* Track info */}
                <Box sx={{ flex: 1, minWidth: 0 }}>
                  <Typography
                    sx={{
                      fontSize: '13px',
                      fontWeight: 600,
                      color: '#FFFFFF',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                      lineHeight: 1.3,
                    }}
                  >
                    {item.title}
                  </Typography>
                  <Typography
                    sx={{
                      fontSize: '11px',
                      color: 'rgba(255,255,255,0.4)',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                      lineHeight: 1.3,
                    }}
                  >
                    {item.artist} · {timeAgo(item.played_at)}
                  </Typography>
                </Box>

                {/* Play again */}
                <IconButton
                  size="small"
                  onClick={() => onTrackSelect(item.track_id)}
                  sx={{
                    width: 32,
                    height: 32,
                    background: 'rgba(124,58,237,0.3)',
                    color: '#A78BFA',
                    flexShrink: 0,
                    '&:hover': {
                      background: 'rgba(124,58,237,0.5)',
                      color: '#FFFFFF',
                    },
                  }}
                >
                  <PlayArrowIcon sx={{ fontSize: 18 }} />
                </IconButton>
              </Box>
            ))}
          </Box>
        )}
      </Box>
    </Drawer>
  );
};
