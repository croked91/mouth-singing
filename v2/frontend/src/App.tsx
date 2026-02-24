import React from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { ThemeProvider, CssBaseline } from '@mui/material';
import { darkTheme } from './theme/darkTheme';
import { WelcomePage } from './pages/WelcomePage';
import { SessionPage } from './pages/SessionPage';
import { QueuePage } from './pages/QueuePage';
import { PlayerPage } from './pages/PlayerPage';
import { AdminPage } from './pages/AdminPage';

const App: React.FC = () => {
  return (
    <ThemeProvider theme={darkTheme}>
      <CssBaseline />
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<WelcomePage />} />
          <Route path="/session/:id" element={<SessionPage />} />
          <Route path="/session/:id/queue" element={<QueuePage />} />
          <Route path="/session/:id/play/:entryId" element={<PlayerPage />} />
          <Route path="/admin" element={<AdminPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </ThemeProvider>
  );
};

export default App;
