import { create } from 'zustand';

interface PlayerState {
  isPlaying: boolean;
  currentTime: number;
  volume: number;

  setPlaying: (playing: boolean) => void;
  setCurrentTime: (time: number) => void;
  setVolume: (volume: number) => void;
  reset: () => void;
}

export const usePlayerStore = create<PlayerState>((set) => ({
  isPlaying: false,
  currentTime: 0,
  volume: 1,

  setPlaying: (playing: boolean): void => {
    set({ isPlaying: playing });
  },

  setCurrentTime: (time: number): void => {
    set({ currentTime: time });
  },

  setVolume: (volume: number): void => {
    set({ volume: Math.max(0, Math.min(1, volume)) });
  },

  reset: (): void => {
    set({ isPlaying: false, currentTime: 0, volume: 1 });
  },
}));
