import React from 'react';
import { Box, Typography, ButtonBase } from '@mui/material';
import MusicNoteIcon from '@mui/icons-material/MusicNote';
import type { RecommendedTrackItem } from '../types';

interface TrackCardProps {
  track: RecommendedTrackItem;
  onSelect: (trackId: string) => void;
  isAdding?: boolean;
}

export const TrackCard: React.FC<TrackCardProps> = ({
  track,
  onSelect,
  isAdding = false,
}) => {
  const handleSelect = (): void => {
    if (!isAdding) {
      onSelect(track.id);
    }
  };

  return (
    <Box
      sx={{
        height: 72,
        width: '100%',
        display: 'flex',
        alignItems: 'center',
        gap: 1.5,
        px: 1.5,
        background: 'rgba(255,255,255,0.06)',
        border: '1px solid rgba(255,255,255,0.10)',
        borderRadius: '14px',
        transition: 'border-color 0.2s ease, background 0.2s ease',
        '&:hover': {
          borderColor: 'rgba(167,139,250,0.5)',
          background: 'rgba(124,58,237,0.15)',
        },
      }}
    >
      {/* Album art placeholder */}
      <Box
        sx={{
          width: 44,
          height: 44,
          borderRadius: '10px',
          background: 'linear-gradient(135deg, rgba(124,58,237,0.5), rgba(37,99,235,0.5))',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          flexShrink: 0,
        }}
      >
        <MusicNoteIcon sx={{ color: 'rgba(255,255,255,0.7)', fontSize: 20 }} />
      </Box>

      {/* Track info */}
      <Box sx={{ flex: 1, minWidth: 0 }}>
        <Typography
          sx={{
            fontSize: '15px',
            fontWeight: 600,
            color: '#FFFFFF',
            lineHeight: 1.3,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {track.title}
        </Typography>
        <Typography
          sx={{
            fontSize: '13px',
            fontWeight: 400,
            color: 'rgba(255,255,255,0.5)',
            lineHeight: 1.3,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {track.artist}
        </Typography>
      </Box>

      {/* Select button */}
      <ButtonBase
        onClick={handleSelect}
        disabled={isAdding}
        sx={{
          px: 1.75,
          py: 0.625,
          borderRadius: '20px',
          background: 'rgba(124,58,237,0.4)',
          border: '1px solid rgba(167,139,250,0.35)',
          color: '#A78BFA',
          fontSize: '11px',
          fontWeight: 700,
          letterSpacing: '0.08em',
          textTransform: 'uppercase',
          flexShrink: 0,
          transition: 'background 0.2s ease, border-color 0.2s ease',
          '&:hover': {
            background: 'rgba(124,58,237,0.6)',
            borderColor: 'rgba(167,139,250,0.6)',
          },
          '&:disabled': {
            opacity: 0.45,
            cursor: 'not-allowed',
          },
        }}
      >
        ВЫБРАТЬ
      </ButtonBase>
    </Box>
  );
};
