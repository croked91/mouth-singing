import { create } from 'zustand';
import { api } from '../services/api';
import type { Participant } from '../types';

interface SessionState {
  sessionId: string | null;
  roomId: string | null;
  participants: Participant[];
  isLoading: boolean;
  error: string | null;

  createSession: (roomId: string) => Promise<string>;
  loadSession: (sessionId: string) => Promise<void>;
  addParticipant: (name?: string) => Promise<Participant>;
  removeParticipantLocally: (participantId: string) => void;
  clearError: () => void;
}

export const useSessionStore = create<SessionState>((set, get) => ({
  sessionId: null,
  roomId: null,
  participants: [],
  isLoading: false,
  error: null,

  createSession: async (roomId: string): Promise<string> => {
    set({ isLoading: true, error: null });
    try {
      const session = await api.createSession(roomId);
      set({
        sessionId: session.id,
        roomId: session.room_id,
        participants: [],
        isLoading: false,
      });
      return session.id;
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Ошибка создания сессии';
      set({ isLoading: false, error: message });
      throw err;
    }
  },

  loadSession: async (sessionId: string): Promise<void> => {
    set({ isLoading: true, error: null });
    try {
      const session = await api.getSession(sessionId);
      set({
        sessionId: session.id,
        roomId: session.room_id,
        participants: session.participants ?? [],
        isLoading: false,
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Ошибка загрузки сессии';
      set({ isLoading: false, error: message });
      throw err;
    }
  },

  addParticipant: async (name?: string): Promise<Participant> => {
    const { sessionId } = get();
    if (!sessionId) {
      throw new Error('Сессия не найдена');
    }
    set({ isLoading: true, error: null });
    try {
      const participant = await api.addParticipant(sessionId, name);
      set((state) => ({
        participants: [...state.participants, participant],
        isLoading: false,
      }));
      return participant;
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Ошибка добавления участника';
      set({ isLoading: false, error: message });
      throw err;
    }
  },

  removeParticipantLocally: (participantId: string): void => {
    set((state) => ({
      participants: state.participants.filter((p) => p.id !== participantId),
    }));
  },

  clearError: (): void => {
    set({ error: null });
  },
}));
