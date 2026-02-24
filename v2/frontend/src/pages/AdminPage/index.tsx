import React from 'react';
import { Box, Typography } from '@mui/material';
import AdminPanelSettingsIcon from '@mui/icons-material/AdminPanelSettings';
import { CosmicBackground } from '../../components/CosmicBackground';

export const AdminPage: React.FC = () => {
  return (
    <CosmicBackground>
      <Box
        sx={{
          minHeight: '100vh',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 3,
        }}
      >
        <AdminPanelSettingsIcon
          sx={{ fontSize: 64, color: 'rgba(124,58,237,0.6)' }}
        />
        <Typography
          variant="h3"
          sx={{
            fontWeight: 700,
            background: 'linear-gradient(135deg, #A78BFA, #60A5FA)',
            WebkitBackgroundClip: 'text',
            WebkitTextFillColor: 'transparent',
            backgroundClip: 'text',
          }}
        >
          Админ (скоро)
        </Typography>
        <Typography variant="body1" sx={{ color: 'rgba(255,255,255,0.45)' }}>
          Эта страница появится в следующей фазе разработки
        </Typography>
      </Box>
    </CosmicBackground>
  );
};
