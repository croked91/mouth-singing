import React, { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { Alert, Box, CircularProgress, Paper, Stack, TextField, Typography } from '@mui/material';
import { api } from '../../services/api';
import { AlignmentEditor } from '../../components/alignment/AlignmentEditor';
import type { AlignmentDocument, AlignmentEditorPayload, AlignmentRevision } from '../../types';

export const AlignmentEditorPage: React.FC = () => {
  const { trackId } = useParams();
  const [payload, setPayload] = useState<AlignmentEditorPayload | null>(null);
  const [adminSecret, setAdminSecret] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!trackId) return;

    api.getTrackAlignment(trackId)
      .then((data) => {
        setPayload(data);
        setError(null);
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoading(false));
  }, [trackId]);

  const saveDraft = async (document: AlignmentDocument): Promise<AlignmentRevision> => {
    if (!trackId) throw new Error('Track id is missing');
    return api.saveAlignmentDraft(
      trackId,
      { document, operations: [], diagnostics: {}, created_by: 'admin' },
      adminSecret,
    );
  };

  const publish = async (revisionId: string): Promise<AlignmentRevision> => {
    if (!trackId) throw new Error('Track id is missing');
    return api.publishAlignment(trackId, revisionId, adminSecret);
  };

  if (!trackId) {
    return (
      <Box sx={{ minHeight: '100vh', p: 3, background: '#09090f' }}>
        <Alert severity="error">Track id is missing</Alert>
      </Box>
    );
  }

  if (loading) {
    return (
      <Box sx={{ minHeight: '100vh', display: 'grid', placeItems: 'center', background: '#09090f' }}>
        <CircularProgress />
      </Box>
    );
  }

  if (error || !payload) {
    return (
      <Box sx={{ minHeight: '100vh', p: 3, background: '#09090f' }}>
        <Alert severity="error">{error ?? 'Не удалось загрузить редактор.'}</Alert>
      </Box>
    );
  }

  return (
    <Box sx={{ background: '#09090f' }}>
      <Paper sx={{ position: 'sticky', top: 0, zIndex: 10, p: 2, borderRadius: 0, background: '#11111a', color: 'white' }}>
        <Stack direction="row" spacing={2} alignItems="center">
          <Typography fontWeight={700}>Alignment editor</Typography>
          <TextField
            label="Admin PIN/secret"
            type="password"
            size="small"
            value={adminSecret}
            onChange={(event) => setAdminSecret(event.target.value)}
            InputProps={{ sx: { color: 'white' } }}
            InputLabelProps={{ sx: { color: 'rgba(255,255,255,0.7)' } }}
          />
        </Stack>
      </Paper>
      <AlignmentEditor
        payload={payload}
        adminSecret={adminSecret}
        onSaveDraft={saveDraft}
        onPublish={publish}
      />
    </Box>
  );
};
