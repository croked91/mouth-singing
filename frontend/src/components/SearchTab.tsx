import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  Box,
  Typography,
  InputBase,
  IconButton,
  Skeleton,
  ButtonBase,
  List,
  ListItem,
  ListItemButton,
  Paper,
} from '@mui/material';
import SearchIcon from '@mui/icons-material/Search';
import CloseIcon from '@mui/icons-material/Close';
import SearchOffIcon from '@mui/icons-material/SearchOff';
import MusicNoteIcon from '@mui/icons-material/MusicNote';

import { api } from '../services/api';
import type { TrackSearchItem } from '../types';

// ─── Constants ────────────────────────────────────────────────────────────────

const DEBOUNCE_MS = 300;

// ─── Helpers ──────────────────────────────────────────────────────────────────

function formatDuration(seconds: number | null): string {
  if (seconds === null) return '--:--';
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${s.toString().padStart(2, '0')}`;
}

// ─── Props ────────────────────────────────────────────────────────────────────

interface SearchTabProps {
  sessionId: string;
  onTrackSelected: (trackId: string) => void | Promise<void>;
}

// ─── SearchResultCard ─────────────────────────────────────────────────────────

interface SearchResultCardProps {
  track: TrackSearchItem;
  index: number;
  onSelect: (trackId: string) => void;
  isAdding: boolean;
}

const SearchResultCard: React.FC<SearchResultCardProps> = ({
  track,
  index,
  onSelect,
  isAdding,
}) => (
  <Box
    sx={{
      display: 'flex',
      alignItems: 'center',
      gap: 1.5,
      px: 1.5,
      py: 1,
      background: 'rgba(255,255,255,0.05)',
      border: '1px solid rgba(255,255,255,0.09)',
      borderRadius: '14px',
      transition: 'border-color 0.2s ease, background 0.2s ease',
      '&:hover': {
        borderColor: 'rgba(6,182,212,0.4)',
        background: 'rgba(6,182,212,0.07)',
      },
    }}
  >
    {/* Index */}
    <Typography
      sx={{
        fontSize: '13px',
        fontWeight: 700,
        color: 'rgba(255,255,255,0.25)',
        minWidth: 22,
        textAlign: 'center',
        flexShrink: 0,
      }}
    >
      {index + 1}
    </Typography>

    {/* Album art placeholder */}
    <Box
      sx={{
        width: 44,
        height: 44,
        borderRadius: '10px',
        background: 'linear-gradient(135deg, rgba(6,182,212,0.45), rgba(124,58,237,0.45))',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        flexShrink: 0,
      }}
    >
      <MusicNoteIcon sx={{ color: 'rgba(255,255,255,0.7)', fontSize: 20 }} />
    </Box>

    {/* Track info */}
    <Box sx={{ flex: 1, minWidth: 0 }}>
      <Typography
        sx={{
          fontSize: '14px',
          fontWeight: 600,
          color: '#FFFFFF',
          lineHeight: 1.3,
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
      >
        {track.title}
      </Typography>
      <Typography
        sx={{
          fontSize: '12px',
          color: 'rgba(255,255,255,0.5)',
          lineHeight: 1.3,
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
      >
        {track.artist}
      </Typography>
    </Box>

    {/* Duration */}
    <Typography
      sx={{
        fontSize: '12px',
        color: 'rgba(255,255,255,0.35)',
        flexShrink: 0,
        minWidth: 36,
        textAlign: 'right',
      }}
    >
      {formatDuration(track.duration_sec)}
    </Typography>

    {/* Select button */}
    <ButtonBase
      onClick={() => onSelect(track.id)}
      disabled={isAdding}
      sx={{
        px: 1.75,
        py: 0.625,
        borderRadius: '20px',
        background: 'rgba(6,182,212,0.25)',
        border: '1px solid rgba(6,182,212,0.45)',
        color: '#67E8F9',
        fontSize: '11px',
        fontWeight: 700,
        letterSpacing: '0.08em',
        textTransform: 'uppercase',
        flexShrink: 0,
        transition: 'background 0.2s ease, border-color 0.2s ease',
        '&:hover': {
          background: 'rgba(6,182,212,0.4)',
          borderColor: 'rgba(6,182,212,0.7)',
        },
        '&:disabled': {
          opacity: 0.4,
          cursor: 'not-allowed',
        },
      }}
    >
      ВЫБРАТЬ
    </ButtonBase>
  </Box>
);

// ─── SkeletonCard ─────────────────────────────────────────────────────────────

const SkeletonCard: React.FC = () => (
  <Box
    sx={{
      display: 'flex',
      alignItems: 'center',
      gap: 1.5,
      px: 1.5,
      py: 1,
      background: 'rgba(255,255,255,0.04)',
      border: '1px solid rgba(255,255,255,0.07)',
      borderRadius: '14px',
    }}
  >
    <Skeleton variant="rectangular" width={22} height={16} sx={{ bgcolor: 'rgba(255,255,255,0.08)', borderRadius: 1 }} />
    <Skeleton variant="rectangular" width={44} height={44} sx={{ bgcolor: 'rgba(255,255,255,0.08)', borderRadius: '10px', flexShrink: 0 }} />
    <Box sx={{ flex: 1, minWidth: 0 }}>
      <Skeleton variant="text" width="65%" sx={{ bgcolor: 'rgba(255,255,255,0.08)', mb: 0.5 }} />
      <Skeleton variant="text" width="40%" sx={{ bgcolor: 'rgba(255,255,255,0.08)' }} />
    </Box>
    <Skeleton variant="rectangular" width={60} height={28} sx={{ bgcolor: 'rgba(255,255,255,0.08)', borderRadius: '20px', flexShrink: 0 }} />
  </Box>
);

// ─── SearchTab ────────────────────────────────────────────────────────────────

export const SearchTab: React.FC<SearchTabProps> = ({
  onTrackSelected,
}) => {
  const [query, setQuery] = useState('');
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [results, setResults] = useState<TrackSearchItem[] | null>(null);
  const [resultsTotal, setResultsTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [addingTrackId, setAddingTrackId] = useState<string | null>(null);
  const [searched, setSearched] = useState(false);

  const debounceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // ── Suggestion debounce ──────────────────────────────────────────────────

  useEffect(() => {
    if (debounceTimerRef.current !== null) {
      clearTimeout(debounceTimerRef.current);
    }

    const trimmed = query.trim();

    if (!trimmed) {
      setSuggestions([]);
      setShowSuggestions(false);
      return;
    }

    debounceTimerRef.current = setTimeout(() => {
      void api.suggestTracks(trimmed).then((data) => {
        setSuggestions(data);
        setShowSuggestions(data.length > 0);
      }).catch(() => {
        setSuggestions([]);
        setShowSuggestions(false);
      });
    }, DEBOUNCE_MS);

    return () => {
      if (debounceTimerRef.current !== null) {
        clearTimeout(debounceTimerRef.current);
      }
    };
  }, [query]);

  // ── Search ───────────────────────────────────────────────────────────────

  const runSearch = useCallback(async (q: string): Promise<void> => {
    const trimmed = q.trim();
    if (!trimmed) return;

    setShowSuggestions(false);
    setLoading(true);
    setSearched(true);

    try {
      const data = await api.searchTracks(trimmed);
      setResults(data.items);
      setResultsTotal(data.total);
    } catch {
      setResults([]);
      setResultsTotal(0);
    } finally {
      setLoading(false);
    }
  }, []);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>): void => {
    if (e.key === 'Enter') {
      void runSearch(query);
    }
    if (e.key === 'Escape') {
      setShowSuggestions(false);
    }
  };

  const handleSuggestionClick = (suggestion: string): void => {
    setQuery(suggestion);
    setShowSuggestions(false);
    void runSearch(suggestion);
  };

  const handleClear = (): void => {
    setQuery('');
    setSuggestions([]);
    setShowSuggestions(false);
    setResults(null);
    setSearched(false);
    inputRef.current?.focus();
  };

  const handleTrackSelect = async (trackId: string): Promise<void> => {
    setAddingTrackId(trackId);
    try {
      await onTrackSelected(trackId);
    } finally {
      setAddingTrackId(null);
    }
  };

  // ── Render ───────────────────────────────────────────────────────────────

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2.5 }}>
      {/* Search input */}
      <Box sx={{ position: 'relative' }}>
        <Box
          sx={{
            display: 'flex',
            alignItems: 'center',
            height: 48,
            borderRadius: '24px',
            background: 'rgba(255,255,255,0.06)',
            border: '1px solid rgba(255,255,255,0.12)',
            px: 2,
            gap: 1.25,
            transition: 'border-color 0.2s ease',
            '&:focus-within': {
              borderColor: 'rgba(6,182,212,0.55)',
              background: 'rgba(6,182,212,0.05)',
            },
          }}
        >
          <SearchIcon sx={{ color: 'rgba(255,255,255,0.35)', fontSize: 20, flexShrink: 0 }} />

          <InputBase
            inputRef={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={handleKeyDown}
            onFocus={() => {
              if (suggestions.length > 0) setShowSuggestions(true);
            }}
            placeholder="Исполнитель, название, текст..."
            fullWidth
            sx={{
              color: '#FFFFFF',
              fontSize: '14px',
              '& input': {
                padding: 0,
                '&::placeholder': {
                  color: 'rgba(255,255,255,0.3)',
                  opacity: 1,
                },
              },
            }}
          />

          {query && (
            <IconButton
              size="small"
              onClick={handleClear}
              sx={{ color: 'rgba(255,255,255,0.35)', p: 0.25, flexShrink: 0 }}
            >
              <CloseIcon sx={{ fontSize: 18 }} />
            </IconButton>
          )}
        </Box>

        {/* Suggestions dropdown */}
        {showSuggestions && suggestions.length > 0 && (
          <Paper
            elevation={8}
            sx={{
              position: 'absolute',
              top: 'calc(100% + 6px)',
              left: 0,
              right: 0,
              zIndex: 10,
              background: 'rgba(15,10,40,0.97)',
              border: '1px solid rgba(6,182,212,0.3)',
              borderRadius: '16px',
              overflow: 'hidden',
              backdropFilter: 'blur(20px)',
            }}
          >
            <List dense disablePadding>
              {suggestions.map((s, i) => (
                <ListItem key={i} disablePadding>
                  <ListItemButton
                    onClick={() => handleSuggestionClick(s)}
                    sx={{
                      px: 2,
                      py: 1,
                      gap: 1.5,
                      '&:hover': {
                        background: 'rgba(6,182,212,0.12)',
                      },
                    }}
                  >
                    <SearchIcon sx={{ fontSize: 16, color: 'rgba(6,182,212,0.6)', flexShrink: 0 }} />
                    <Typography sx={{ fontSize: '14px', color: 'rgba(255,255,255,0.85)' }}>
                      {s}
                    </Typography>
                  </ListItemButton>
                </ListItem>
              ))}
            </List>
          </Paper>
        )}
      </Box>

      {/* Loading skeletons */}
      {loading && (
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
          {Array.from({ length: 6 }).map((_, i) => (
            <SkeletonCard key={i} />
          ))}
        </Box>
      )}

      {/* Results */}
      {!loading && results !== null && results.length > 0 && (
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
          {/* Header */}
          <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 0.5 }}>
            <Typography
              sx={{
                fontSize: '11px',
                fontWeight: 700,
                letterSpacing: '0.12em',
                color: 'rgba(255,255,255,0.35)',
                textTransform: 'uppercase',
              }}
            >
              РЕЗУЛЬТАТЫ
            </Typography>
            <Typography
              sx={{
                fontSize: '12px',
                color: 'rgba(255,255,255,0.4)',
              }}
            >
              {resultsTotal} треков найдено
            </Typography>
          </Box>

          {results.map((track, i) => (
            <SearchResultCard
              key={track.id}
              track={track}
              index={i}
              onSelect={(id) => { void handleTrackSelect(id); }}
              isAdding={addingTrackId === track.id}
            />
          ))}
        </Box>
      )}

      {/* Empty state */}
      {!loading && searched && results !== null && results.length === 0 && (
        <Box
          sx={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            py: 8,
            gap: 1.5,
          }}
        >
          <SearchOffIcon sx={{ fontSize: 48, color: 'rgba(255,255,255,0.15)' }} />
          <Typography
            sx={{
              fontSize: '16px',
              fontWeight: 600,
              color: 'rgba(255,255,255,0.45)',
            }}
          >
            Ничего не найдено
          </Typography>
          <Typography
            sx={{
              fontSize: '13px',
              color: 'rgba(255,255,255,0.3)',
              textAlign: 'center',
              maxWidth: 280,
            }}
          >
            Попробуйте другой запрос или загрузите свой трек
          </Typography>
        </Box>
      )}

      {/* Initial state — no search yet */}
      {!loading && !searched && (
        <Box
          sx={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            py: 8,
            gap: 1.5,
          }}
        >
          <Box
            sx={{
              width: 64,
              height: 64,
              borderRadius: '50%',
              background: 'linear-gradient(135deg, rgba(6,182,212,0.2), rgba(124,58,237,0.2))',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
            }}
          >
            <SearchIcon sx={{ fontSize: 30, color: 'rgba(6,182,212,0.6)' }} />
          </Box>
          <Typography
            sx={{
              fontSize: '14px',
              color: 'rgba(255,255,255,0.3)',
              textAlign: 'center',
            }}
          >
            Начните вводить для поиска
          </Typography>
          <Typography
            sx={{
              fontSize: '12px',
              color: 'rgba(255,255,255,0.2)',
              textAlign: 'center',
              maxWidth: 280,
            }}
          >
            Поиск по исполнителю, названию или тексту песни
          </Typography>
        </Box>
      )}
    </Box>
  );
};
