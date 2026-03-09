import React, { useMemo } from 'react';
import { Box } from '@mui/material';

interface StarDot {
  id: number;
  top: string;
  left: string;
  size: number;
  opacity: number;
  animationDelay: string;
  animationDuration: string;
}

function generateStars(count: number): StarDot[] {
  return Array.from({ length: count }, (_, i) => ({
    id: i,
    top: `${Math.random() * 100}%`,
    left: `${Math.random() * 100}%`,
    size: Math.random() * 2 + 1,
    opacity: Math.random() * 0.6 + 0.1,
    animationDelay: `${Math.random() * 4}s`,
    animationDuration: `${Math.random() * 3 + 2}s`,
  }));
}

interface CosmicBackgroundProps {
  children?: React.ReactNode;
}

export const CosmicBackground: React.FC<CosmicBackgroundProps> = ({
  children,
}) => {
  const stars = useMemo(() => generateStars(55), []);

  return (
    <Box
      sx={{
        position: 'relative',
        minHeight: '100vh',
        backgroundColor: '#050508',
        overflow: 'hidden',
      }}
    >
      {/* Base gradient */}
      <Box
        sx={{
          position: 'absolute',
          inset: 0,
          background:
            'radial-gradient(ellipse at 50% 0%, rgba(124,58,237,0.18) 0%, transparent 60%), radial-gradient(ellipse at 100% 100%, rgba(37,99,235,0.14) 0%, transparent 50%), radial-gradient(ellipse at 0% 80%, rgba(6,182,212,0.08) 0%, transparent 45%)',
          pointerEvents: 'none',
          zIndex: 0,
        }}
      />

      {/* Glow blob — violet top-left */}
      <Box
        sx={{
          position: 'absolute',
          top: '-120px',
          left: '-80px',
          width: '500px',
          height: '500px',
          borderRadius: '50%',
          background: 'radial-gradient(circle, rgba(124,58,237,0.22) 0%, transparent 70%)',
          filter: 'blur(40px)',
          pointerEvents: 'none',
          zIndex: 0,
        }}
      />

      {/* Glow blob — blue bottom-right */}
      <Box
        sx={{
          position: 'absolute',
          bottom: '-100px',
          right: '-60px',
          width: '420px',
          height: '420px',
          borderRadius: '50%',
          background: 'radial-gradient(circle, rgba(37,99,235,0.18) 0%, transparent 70%)',
          filter: 'blur(40px)',
          pointerEvents: 'none',
          zIndex: 0,
        }}
      />

      {/* Glow blob — cyan center */}
      <Box
        sx={{
          position: 'absolute',
          top: '40%',
          left: '50%',
          transform: 'translate(-50%, -50%)',
          width: '300px',
          height: '300px',
          borderRadius: '50%',
          background: 'radial-gradient(circle, rgba(6,182,212,0.07) 0%, transparent 70%)',
          filter: 'blur(60px)',
          pointerEvents: 'none',
          zIndex: 0,
        }}
      />

      {/* Star dots */}
      {stars.map((star) => (
        <Box
          key={star.id}
          sx={{
            position: 'absolute',
            top: star.top,
            left: star.left,
            width: `${star.size}px`,
            height: `${star.size}px`,
            borderRadius: '50%',
            backgroundColor: '#FFFFFF',
            opacity: star.opacity,
            pointerEvents: 'none',
            zIndex: 0,
            '@keyframes twinkle': {
              '0%, 100%': { opacity: star.opacity },
              '50%': { opacity: star.opacity * 0.3 },
            },
            animation: `twinkle ${star.animationDuration} ${star.animationDelay} ease-in-out infinite`,
          }}
        />
      ))}

      {/* SVG noise filter */}
      <Box
        component="svg"
        sx={{
          position: 'absolute',
          width: 0,
          height: 0,
          pointerEvents: 'none',
        }}
      >
        <defs>
          <filter id="noise">
            <feTurbulence
              type="fractalNoise"
              baseFrequency="0.65"
              numOctaves="3"
              stitchTiles="stitch"
            />
            <feColorMatrix type="saturate" values="0" />
            <feBlend in="SourceGraphic" mode="overlay" result="blend" />
            <feComposite in="blend" in2="SourceGraphic" operator="in" />
          </filter>
        </defs>
      </Box>

      {/* Noise overlay */}
      <Box
        sx={{
          position: 'absolute',
          inset: 0,
          opacity: 0.03,
          filter: 'url(#noise)',
          pointerEvents: 'none',
          zIndex: 0,
        }}
      />

      {/* Content */}
      <Box sx={{ position: 'relative', zIndex: 1 }}>{children}</Box>
    </Box>
  );
};
