import React, { useEffect, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import { Alert, Box, Button, CircularProgress, Dialog, DialogActions, DialogContent, DialogTitle, TextField } from '@mui/material';
import { api } from '../../services/api';
import { AlignmentEditor } from '../../components/alignment/AlignmentEditor';
import type {
  AlignmentDocument,
  AlignmentEditorPayload,
  AlignmentRevision,
  AutoRepairMode,
  AutoRepairReport,
  RealignSyllablesFragmentJobResponse,
  RealignSyllablesFragmentRequest,
} from '../../types';

const ALIGNMENT_ADMIN_SECRET_STORAGE_KEY = 'alignmentAdminSecret';

export const AlignmentEditorPage: React.FC = () => {
  const { trackId } = useParams();
  const [payload, setPayload] = useState<AlignmentEditorPayload | null>(null);
  const [adminSecret, setAdminSecret] = useState(() => window.sessionStorage.getItem(ALIGNMENT_ADMIN_SECRET_STORAGE_KEY) ?? '');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [adminDialogOpen, setAdminDialogOpen] = useState(false);
  const [adminDraft, setAdminDraft] = useState('');
  const adminResolverRef = useRef<((value: boolean) => void) | null>(null);
  const adminSecretRef = useRef(adminSecret);

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

  useEffect(() => {
    adminSecretRef.current = adminSecret;
    if (adminSecret) window.sessionStorage.setItem(ALIGNMENT_ADMIN_SECRET_STORAGE_KEY, adminSecret);
    else window.sessionStorage.removeItem(ALIGNMENT_ADMIN_SECRET_STORAGE_KEY);
  }, [adminSecret]);

  const requestAdminSecret = (): Promise<boolean> => {
    const currentSecret = adminSecretRef.current || window.sessionStorage.getItem(ALIGNMENT_ADMIN_SECRET_STORAGE_KEY) || '';
    if (currentSecret) {
      if (currentSecret !== adminSecretRef.current) {
        adminSecretRef.current = currentSecret;
        setAdminSecret(currentSecret);
      }
      return Promise.resolve(true);
    }
    setAdminDraft('');
    setAdminDialogOpen(true);
    return new Promise<boolean>((resolve) => {
      adminResolverRef.current = resolve;
    });
  };

  const closeAdminDialog = (granted: boolean): void => {
    setAdminDialogOpen(false);
    const resolve = adminResolverRef.current;
    adminResolverRef.current = null;
    resolve?.(granted);
  };

  const confirmAdminDialog = (): void => {
    const nextSecret = adminDraft.trim();
    if (!nextSecret) return;
    adminSecretRef.current = nextSecret;
    window.sessionStorage.setItem(ALIGNMENT_ADMIN_SECRET_STORAGE_KEY, nextSecret);
    setAdminSecret(nextSecret);
    closeAdminDialog(true);
  };

  const getAdminSecret = (): string => adminSecretRef.current || window.sessionStorage.getItem(ALIGNMENT_ADMIN_SECRET_STORAGE_KEY) || '';

  const saveDraft = async (
    document: AlignmentDocument,
    operations: Record<string, unknown>[] = [],
    diagnostics: Record<string, unknown> = {},
  ): Promise<AlignmentRevision> => {
    if (!trackId) throw new Error('Track id is missing');
    return api.saveAlignmentDraft(
      trackId,
      { document, operations, diagnostics, created_by: 'admin' },
      getAdminSecret(),
    );
  };

  const realignLyrics = async (lyricsText: string): Promise<string> => {
    if (!trackId) throw new Error('Track id is missing');
    const response = await api.realignLyrics(trackId, lyricsText, getAdminSecret());
    return response.job_id;
  };

  const realignSyllablesForFragment = async (
    request: RealignSyllablesFragmentRequest,
  ): Promise<RealignSyllablesFragmentJobResponse> => {
    if (!trackId) throw new Error('Track id is missing');
    return api.realignSyllablesForFragment(trackId, request, getAdminSecret());
  };

  const startAutoRepair = async (
    revisionId: string,
    mode: AutoRepairMode,
  ): Promise<string> => {
    if (!trackId) throw new Error('Track id is missing');
    const response = await api.startAlignmentAutoRepair(
      trackId,
      { revision_id: revisionId, mode },
      getAdminSecret(),
    );
    return response.job_id;
  };

  const getAutoRepairReport = async (jobId: string): Promise<AutoRepairReport> => {
    return api.getJobResult<AutoRepairReport>(jobId);
  };

  const applyAutoRepair = async (
    jobId: string,
    baseRevisionId: string,
    proposalIds: string[],
  ): Promise<AlignmentRevision> => {
    if (!trackId) throw new Error('Track id is missing');
    return api.applyAlignmentAutoRepair(
      trackId,
      { job_id: jobId, base_revision_id: baseRevisionId, proposal_ids: proposalIds, created_by: 'admin' },
      getAdminSecret(),
    );
  };

  const reloadPayload = async (): Promise<void> => {
    if (!trackId) throw new Error('Track id is missing');
    const data = await api.getTrackAlignment(trackId);
    setPayload(data);
    setError(null);
  };

  const publish = async (revisionId: string): Promise<AlignmentRevision> => {
    if (!trackId) throw new Error('Track id is missing');
    return api.publishAlignment(trackId, revisionId, getAdminSecret());
  };

  const restoreRevision = async (revisionId: string): Promise<AlignmentRevision> => {
    if (!trackId) throw new Error('Track id is missing');
    return api.restoreAlignmentRevision(trackId, revisionId, getAdminSecret());
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
      <AlignmentEditor
        payload={payload}
        adminSecret={adminSecret}
        onRequestAdminSecret={requestAdminSecret}
        onOpenAdminSecretDialog={() => {
          setAdminDraft(getAdminSecret());
          setAdminDialogOpen(true);
        }}
        onSaveDraft={saveDraft}
        onRealignLyrics={realignLyrics}
        onRealignSyllablesForFragment={realignSyllablesForFragment}
        onStartAutoRepair={startAutoRepair}
        onGetAutoRepairReport={getAutoRepairReport}
        onApplyAutoRepair={applyAutoRepair}
        onReload={reloadPayload}
        onPublish={publish}
        onRestoreRevision={restoreRevision}
      />
      <Dialog open={adminDialogOpen} onClose={() => closeAdminDialog(false)} fullWidth maxWidth="xs">
        <DialogTitle>Введите admin PIN/secret</DialogTitle>
        <DialogContent>
          <TextField
            fullWidth
            autoFocus
            type="password"
            margin="dense"
            label="Admin PIN/secret"
            value={adminDraft}
            onChange={(event) => setAdminDraft(event.target.value)}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => closeAdminDialog(false)}>Отмена</Button>
          <Button variant="contained" onClick={confirmAdminDialog} disabled={!adminDraft.trim()}>
            Продолжить
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
};
