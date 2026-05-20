import axios from 'axios';
import type {
  Session,
  SessionWithParticipants,
  Participant,
  QueueResponse,
  QueueEntry,
  RecommendationResponse,
  MoodTag,
  SearchResult,
  UploadResponse,
  ActiveJob,
  StartPlayingResponse,
  FinishPlayingResponse,
  HistoryItem,
  AlignmentEditorPayload,
  AlignmentRevision,
  AlignmentReviewQueueItem,
  RealignLyricsResponse,
  RealignSyllablesFragmentRequest,
  RealignSyllablesFragmentJobResponse,
  SaveAlignmentDraftRequest,
  AutoRepairAlignmentRequest,
  AutoRepairJobResponse,
  ApplyAutoRepairRequest,
} from '../types';

const apiClient = axios.create({
  baseURL: '/api/v1',
  timeout: 15000,
  headers: {
    'Content-Type': 'application/json',
  },
});

apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    // Network-level error (server unreachable, DNS failure, etc.)
    if (!error.response) {
      if (error.code === 'ECONNABORTED' || error.message?.toLowerCase().includes('timeout')) {
        return Promise.reject(new Error('Сервер не отвечает'));
      }
      return Promise.reject(new Error('Нет подключения к серверу'));
    }

    const status: number = error.response.status;
    const serverDetail: string | undefined =
      error.response.data?.detail || error.response.data?.message;

    let message: string;
    if (status === 403) {
      message = 'Доступ запрещён';
    } else if (status === 404) {
      message = serverDetail ?? 'Не найдено';
    } else if (status >= 500) {
      message = serverDetail ?? 'Что-то пошло не так, попробуйте позже';
    } else {
      message = serverDetail ?? error.message ?? 'Произошла ошибка';
    }

    return Promise.reject(new Error(message));
  }
);

export const api = {
  createSession: async (roomId: string): Promise<Session> => {
    const response = await apiClient.post<Session>('/sessions', {
      room_id: roomId,
    });
    return response.data;
  },

  getSession: async (sessionId: string): Promise<SessionWithParticipants> => {
    const response = await apiClient.get<SessionWithParticipants>(
      `/sessions/${sessionId}`
    );
    return response.data;
  },

  addParticipant: async (
    sessionId: string,
    name?: string
  ): Promise<Participant> => {
    const response = await apiClient.post<Participant>(
      `/sessions/${sessionId}/participants`,
      { name: name || null }
    );
    return response.data;
  },

  getQueue: async (sessionId: string): Promise<QueueResponse> => {
    const response = await apiClient.get<QueueResponse>(
      `/sessions/${sessionId}/queue`
    );
    return response.data;
  },

  addToQueue: async (
    sessionId: string,
    participantId: string,
    trackId: string
  ): Promise<QueueEntry> => {
    const response = await apiClient.post<QueueEntry>('/queue', {
      session_id: sessionId,
      participant_id: participantId,
      track_id: trackId,
    });
    return response.data;
  },

  skipTurn: async (entryId: string): Promise<QueueEntry> => {
    const response = await apiClient.post<QueueEntry>(
      `/queue/${entryId}/skip`
    );
    return response.data;
  },

  startPlaying: async (entryId: string): Promise<StartPlayingResponse> => {
    const response = await apiClient.post<StartPlayingResponse>(
      `/queue/${entryId}/start`
    );
    return response.data;
  },

  finishPlaying: async (entryId: string): Promise<FinishPlayingResponse> => {
    const response = await apiClient.post<FinishPlayingResponse>(
      `/queue/${entryId}/finish`
    );
    return response.data;
  },

  getRecommendations: async (
    sessionId: string,
    limit?: number,
    tagId?: number,
    language?: string,
    excludeIds?: string[],
  ): Promise<RecommendationResponse> => {
    const params: Record<string, string | number> = {
      session_id: sessionId,
      limit: limit ?? 10,
    };
    if (tagId !== undefined) params.tag_id = tagId;
    if (language) params.language = language;
    if (excludeIds?.length) params.exclude_ids = excludeIds.join(',');
    const response = await apiClient.get<RecommendationResponse>(
      '/recommendations',
      { params }
    );
    return response.data;
  },

  getTags: async (sessionId: string, limit?: number): Promise<MoodTag[]> => {
    const response = await apiClient.get<MoodTag[]>('/tags', {
      params: { session_id: sessionId, limit: limit ?? 8 },
    });
    return response.data;
  },

  searchTracks: async (query: string, limit?: number, offset?: number, mode?: 'title' | 'mood', sessionId?: string): Promise<SearchResult> => {
    const response = await apiClient.get<SearchResult>('/tracks/search', {
      params: {
        q: query,
        limit: limit ?? 20,
        offset: offset ?? 0,
        ...(mode === 'mood' ? { mode, session_id: sessionId } : {}),
      },
    });
    return response.data;
  },

  suggestTracks: async (query: string, limit?: number): Promise<string[]> => {
    const response = await apiClient.get<string[]>('/tracks/search/suggest', {
      params: { q: query, limit: limit ?? 10 },
    });
    return response.data;
  },

  terminateSession: async (sessionId: string, adminSecret: string): Promise<void> => {
    await apiClient.delete(`/sessions/${sessionId}`, {
      headers: { 'X-Admin-Secret': adminSecret },
    });
  },

  getActiveJobs: async (): Promise<ActiveJob[]> => {
    const response = await apiClient.get<ActiveJob[]>('/jobs/active');
    return response.data;
  },

  directPlay: async (
    sessionId: string,
    participantId: string,
    trackId: string,
  ): Promise<StartPlayingResponse> => {
    const response = await apiClient.post<StartPlayingResponse>(
      `/sessions/${sessionId}/play`,
      { track_id: trackId, participant_id: participantId },
    );
    return response.data;
  },

  getSessionHistory: async (sessionId: string): Promise<HistoryItem[]> => {
    const response = await apiClient.get<{ items: HistoryItem[] }>(
      `/sessions/${sessionId}/history`,
    );
    return response.data.items;
  },

  getTrackAlignment: async (trackId: string): Promise<AlignmentEditorPayload> => {
    const response = await apiClient.get<AlignmentEditorPayload>(
      `/tracks/${trackId}/alignment`,
    );
    return response.data;
  },

  getAlignmentReviewQueue: async (limit?: number): Promise<AlignmentReviewQueueItem[]> => {
    const response = await apiClient.get<AlignmentReviewQueueItem[]>(
      '/tracks/alignment-reviews',
      { params: { status: 'pending', limit: limit ?? 50 } },
    );
    return response.data;
  },

  saveAlignmentDraft: async (
    trackId: string,
    payload: SaveAlignmentDraftRequest,
    adminSecret: string,
  ): Promise<AlignmentRevision> => {
    const response = await apiClient.put<{ revision: AlignmentRevision }>(
      `/tracks/${trackId}/alignment/draft`,
      payload,
      { headers: { 'X-Admin-Secret': adminSecret } },
    );
    return response.data.revision;
  },

  realignLyrics: async (
    trackId: string,
    lyricsText: string,
    adminSecret: string,
  ): Promise<RealignLyricsResponse> => {
    const response = await apiClient.post<RealignLyricsResponse>(
      `/tracks/${trackId}/alignment/realign`,
      { lyrics_text: lyricsText, created_by: 'admin' },
      { headers: { 'X-Admin-Secret': adminSecret } },
    );
    return response.data;
  },

  realignSyllablesForFragment: async (
    trackId: string,
    payload: RealignSyllablesFragmentRequest,
    adminSecret: string,
  ): Promise<RealignSyllablesFragmentJobResponse> => {
    const response = await apiClient.post<RealignSyllablesFragmentJobResponse>(
      `/tracks/${trackId}/alignment/realign-syllables-fragment`,
      payload,
      { headers: { 'X-Admin-Secret': adminSecret } },
    );
    return response.data;
  },

  startAlignmentAutoRepair: async (
    trackId: string,
    payload: AutoRepairAlignmentRequest,
    adminSecret: string,
  ): Promise<AutoRepairJobResponse> => {
    const response = await apiClient.post<AutoRepairJobResponse>(
      `/tracks/${trackId}/alignment/auto-repair`,
      payload,
      { headers: { 'X-Admin-Secret': adminSecret } },
    );
    return response.data;
  },

  getJobResult: async <T>(jobId: string): Promise<T> => {
    const response = await apiClient.get<T>(`/jobs/${jobId}/result`);
    return response.data;
  },

  applyAlignmentAutoRepair: async (
    trackId: string,
    payload: ApplyAutoRepairRequest,
    adminSecret: string,
  ): Promise<AlignmentRevision> => {
    const response = await apiClient.post<{ revision: AlignmentRevision }>(
      `/tracks/${trackId}/alignment/auto-repair/apply`,
      payload,
      { headers: { 'X-Admin-Secret': adminSecret } },
    );
    return response.data.revision;
  },

  publishAlignment: async (
    trackId: string,
    revisionId: string,
    adminSecret: string,
  ): Promise<AlignmentRevision> => {
    const response = await apiClient.post<{ revision: AlignmentRevision }>(
      `/tracks/${trackId}/alignment/publish`,
      { revision_id: revisionId },
      { headers: { 'X-Admin-Secret': adminSecret } },
    );
    return response.data.revision;
  },

  restoreAlignmentRevision: async (
    trackId: string,
    revisionId: string,
    adminSecret: string,
  ): Promise<AlignmentRevision> => {
    const response = await apiClient.post<{ revision: AlignmentRevision }>(
      `/tracks/${trackId}/alignment/revisions/${revisionId}/restore`,
      {},
      { headers: { 'X-Admin-Secret': adminSecret } },
    );
    return response.data.revision;
  },

  uploadTrack: async (file: File, artist?: string, title?: string): Promise<UploadResponse> => {
    const formData = new FormData();
    formData.append('file', file);
    if (artist) formData.append('artist', artist);
    if (title) formData.append('title', title);
    const response = await apiClient.post<UploadResponse>('/tracks/upload', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
    return response.data;
  },
};

export default api;
