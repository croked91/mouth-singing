import axios from 'axios';
import type {
  Session,
  SessionWithParticipants,
  Participant,
  QueueResponse,
  QueueEntry,
  RecommendationResponse,
} from '../types';

const apiClient = axios.create({
  baseURL: '/api/v1',
  headers: {
    'Content-Type': 'application/json',
  },
});

apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    const message =
      error.response?.data?.detail ||
      error.response?.data?.message ||
      error.message ||
      'Произошла ошибка';
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

  startPlaying: async (entryId: string): Promise<QueueEntry> => {
    const response = await apiClient.post<QueueEntry>(
      `/queue/${entryId}/start`
    );
    return response.data;
  },

  finishPlaying: async (entryId: string): Promise<QueueEntry> => {
    const response = await apiClient.post<QueueEntry>(
      `/queue/${entryId}/finish`
    );
    return response.data;
  },

  getRecommendations: async (
    participantId: string,
    sessionId: string,
    limit?: number
  ): Promise<RecommendationResponse> => {
    const response = await apiClient.get<RecommendationResponse>(
      '/recommendations',
      {
        params: {
          participant_id: participantId,
          session_id: sessionId,
          limit: limit ?? 10,
        },
      }
    );
    return response.data;
  },
};

export default api;
