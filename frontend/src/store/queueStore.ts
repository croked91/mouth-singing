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
  skipTurn: (entryId: string, sessionId: string) => Promise<void>;
  clearError: () => void;
}

export const useQueueStore = create<QueueState>((set, get) => ({
  currentEntry: null,
  upcoming: [],
  isLoading: false,
  error: null,

  loadQueue: async (sessionId: string): Promise<void> => {
    // Skip if already loading (prevents concurrent poll + manual refresh)
    if (get().isLoading) return;
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
      // Don't throw — polling should not generate unhandled rejections
    }
  },

  addToQueue: async (
    sessionId: string,
    participantId: string,
    trackId: string
  ): Promise<void> => {
    set({ error: null });
    try {
      await api.addToQueue(sessionId, participantId, trackId);
      await get().loadQueue(sessionId);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Ошибка добавления в очередь';
      set({ isLoading: false, error: message });
      throw err;
    }
  },

  skipTurn: async (entryId: string, sessionId: string): Promise<void> => {
    set({ error: null });
    try {
      await api.skipTurn(entryId);
      await get().loadQueue(sessionId);
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
