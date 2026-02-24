import React from 'react';
import { Box, Tooltip, Typography } from '@mui/material';
import type { QueueEntryWithDetails } from '../types';

const AVATAR_GRADIENTS = [
  'linear-gradient(135deg, #7C3AED, #EC4899)',
  'linear-gradient(135deg, #2563EB, #06B6D4)',
  'linear-gradient(135deg, #059669, #10B981)',
  'linear-gradient(135deg, #D97706, #F59E0B)',
];

function getAvatarGradient(index: number): string {
  return AVATAR_GRADIENTS[index % AVATAR_GRADIENTS.length];
}

function getInitials(name: string): string {
  const words = name.trim().split(/\s+/);
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}

interface QueueItemProps {
  entry: QueueEntryWithDetails;
  /** Index in the upcoming array — drives gradient and position badge */
  index: number;
}

export const QueueItem: React.FC<QueueItemProps> = ({ entry, index }) => {
  const name = entry.participant?.display_name ?? '?';
  const trackTitle = entry.track?.title ?? '—';

  return (
    <Tooltip title={`${name} — ${trackTitle}`} placement="top" arrow>
      <Box
        sx={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          gap: 0.75,
          flexShrink: 0,
        }}
      >
        {/* Avatar with position badge */}
        <Box sx={{ position: 'relative' }}>
          <Box
            sx={{
              width: 56,
              height: 56,
              borderRadius: '50%',
              background: getAvatarGradient(index),
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: '1rem',
              fontWeight: 700,
              color: '#fff',
              boxShadow: '0 2px 12px rgba(0,0,0,0.35)',
            }}
          >
            {getInitials(name)}
          </Box>

          {/* Position number badge */}
          <Box
            sx={{
              position: 'absolute',
              bottom: -2,
              right: -2,
              width: 18,
              height: 18,
              borderRadius: '50%',
              background: '#0D0B2B',
              border: '1.5px solid rgba(167,139,250,0.6)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: '9px',
              fontWeight: 700,
              color: '#A78BFA',
              lineHeight: 1,
            }}
          >
            {index + 1}
          </Box>
        </Box>

        {/* Nickname */}
        <Typography
          sx={{
            fontSize: '11px',
            fontWeight: 500,
            color: 'rgba(255,255,255,0.6)',
            maxWidth: 64,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
            textAlign: 'center',
          }}
        >
          {name}
        </Typography>
      </Box>
    </Tooltip>
  );
};
