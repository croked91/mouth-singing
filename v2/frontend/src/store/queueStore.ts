import { create } from 'zustand';
import { api } from '../services/api';
import type { QueueEntryWithDetails } from '../types';

interface QueueState {
  currentEntry: QueueEntryWithDetails | null;
  upcoming: QueueEntryWithDetails[];
  isLoading: boolean;
  error: string | null;

  loadQueue: (sessionId: string) => Promise<void>;
  addToQueue: (
    sessionId: string,
    participantId: string,
    trackId: string
  ) => Promise<void>;
  skipTurn: (entryId: string) => Promise<void>;
  clearError: () => void;
}

export const useQueueStore = create<QueueState>((set, get) => ({
  currentEntry: null,
  upcoming: [],
  isLoading: false,
  error: null,

  loadQueue: async (sessionId: string): Promise<void> => {
    set({ isLoading: true, error: null });
    try {
      const queue = await api.getQueue(sessionId);
      set({
        currentEntry: queue.current,
        upcoming: queue.upcoming,
        isLoading: false,
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Ошибка загрузки очереди';
      set({ isLoading: false, error: message });
      throw err;
    }
  },

  addToQueue: async (
    sessionId: string,
    participantId: string,
    trackId: string
  ): Promise<void> => {
    set({ isLoading: true, error: null });
    try {
      await api.addToQueue(sessionId, participantId, trackId);
      await get().loadQueue(sessionId);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Ошибка добавления в очередь';
      set({ isLoading: false, error: message });
      throw err;
    }
  },

  skipTurn: async (entryId: string): Promise<void> => {
    set({ isLoading: true, error: null });
    try {
      await api.skipTurn(entryId);
      set((state) => ({
        upcoming: state.upcoming.filter((e) => e.id !== entryId),
        isLoading: false,
      }));
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Ошибка пропуска хода';
      set({ isLoading: false, error: message });
      throw err;
    }
  },

  clearError: (): void => {
    set({ error: null });
  },
}));
