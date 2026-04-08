import React from 'react';
import { Box, Typography } from '@mui/material';
import MusicNoteIcon from '@mui/icons-material/MusicNote';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';
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
      onClick={handleSelect}
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
        cursor: isAdding ? 'not-allowed' : 'pointer',
        transition: 'border-color 0.2s ease, background 0.2s ease',
        '&:hover': {
          borderColor: 'rgba(167,139,250,0.5)',
          background: 'rgba(124,58,237,0.15)',
        },
      }}
    >
      {/* Artist image or gradient placeholder */}
      {track.artist_image_url ? (
        <Box
          component="img"
          src={track.artist_image_url}
          alt={track.artist}
          sx={{
            width: 44,
            height: 44,
            borderRadius: '10px',
            objectFit: 'cover',
            flexShrink: 0,
          }}
        />
      ) : (
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
      )}

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

      {/* Play button */}
      <Box
        sx={{
          width: 36,
          height: 36,
          borderRadius: '50%',
          background: 'linear-gradient(135deg, #7C3AED, #2563EB)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          flexShrink: 0,
          opacity: isAdding ? 0.45 : 1,
          transition: 'transform 0.15s ease',
          '&:hover': { transform: 'scale(1.1)' },
        }}
      >
        <PlayArrowIcon sx={{ fontSize: 20, color: '#FFFFFF' }} />
      </Box>
    </Box>
  );
};
