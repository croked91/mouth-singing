import React from 'react';
import { useNavigate } from 'react-router-dom';
import { CosmicBackground } from '../../components/CosmicBackground';
import { AdminModal } from '../../components/AdminModal';
import { useSessionStore } from '../../store/sessionStore';

export const AdminPage: React.FC = () => {
  const navigate = useNavigate();
  const sessionId = useSessionStore((state) => state.sessionId);

  const handleClose = (): void => {
    navigate(-1);
  };

  return (
    <CosmicBackground>
      <AdminModal
        open={true}
        onClose={handleClose}
        sessionId={sessionId ?? ''}
      />
    </CosmicBackground>
  );
};
