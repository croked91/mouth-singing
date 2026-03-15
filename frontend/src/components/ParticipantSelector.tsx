import React from 'react';
import { Box, ButtonBase, Typography } from '@mui/material';
import type { Participant } from '../types';

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

interface ParticipantSelectorProps {
  participants: Participant[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}

export const ParticipantSelector: React.FC<ParticipantSelectorProps> = ({
  participants,
  selectedId,
  onSelect,
}) => {
  if (participants.length === 0) {
    return null;
  }

  return (
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
        КТО ВЫБИРАЕТ ТРЕК
      </Typography>

      <Box
        sx={{
          display: 'flex',
          gap: 1,
          flexWrap: 'wrap',
        }}
      >
        {participants.map((participant, index) => {
          const isSelected = participant.id === selectedId;
          const gradient = getAvatarGradient(index);

          return (
            <ButtonBase
              key={participant.id}
              onClick={() => onSelect(participant.id)}
              sx={{
                display: 'flex',
                alignItems: 'center',
                gap: 1,
                px: 1.25,
                py: 0.625,
                borderRadius: '20px',
                border: isSelected
                  ? '1px solid rgba(167,139,250,0.7)'
                  : '1px solid rgba(255,255,255,0.12)',
                background: isSelected
                  ? 'rgba(124,58,237,0.3)'
                  : 'rgba(255,255,255,0.05)',
                transition: 'all 0.2s ease',
                '&:hover': {
                  borderColor: 'rgba(167,139,250,0.5)',
                  background: 'rgba(124,58,237,0.2)',
                },
              }}
            >
              {/* Mini avatar */}
              <Box
                sx={{
                  width: 24,
                  height: 24,
                  borderRadius: '50%',
                  background: gradient,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  fontSize: '9px',
                  fontWeight: 700,
                  color: '#fff',
                  flexShrink: 0,
                }}
              >
                {getInitials(participant.display_name)}
              </Box>

              <Typography
                sx={{
                  fontSize: '13px',
                  fontWeight: isSelected ? 600 : 400,
                  color: isSelected ? '#A78BFA' : 'rgba(255,255,255,0.7)',
                  whiteSpace: 'nowrap',
                }}
              >
                {participant.display_name}
              </Typography>
            </ButtonBase>
          );
        })}
      </Box>
    </Box>
  );
};
