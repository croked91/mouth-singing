import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  Box,
  CircularProgress,
  Typography,
  InputBase,
  IconButton,
  Skeleton,
  List,
  ListItem,
  ListItemButton,
  Paper,
  Popper,
  ClickAwayListener,
  ToggleButton,
  ToggleButtonGroup,
} from '@mui/material';
import SearchIcon from '@mui/icons-material/Search';
import CloseIcon from '@mui/icons-material/Close';
import SearchOffIcon from '@mui/icons-material/SearchOff';
import MusicNoteIcon from '@mui/icons-material/MusicNote';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';

import { api } from '../services/api';
import type { TrackSearchItem } from '../types';

// ─── Constants ────────────────────────────────────────────────────────────────

const DEBOUNCE_MS = 300;
const PAGE_SIZE = 20;

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
  onSearchStateChange?: (active: boolean) => void;
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
    onClick={() => { if (!isAdding) onSelect(track.id); }}
    sx={{
      display: 'flex',
      alignItems: 'center',
      gap: 1.5,
      px: 1.5,
      py: 1,
      background: 'rgba(255,255,255,0.05)',
      border: '1px solid rgba(255,255,255,0.09)',
      borderRadius: '14px',
      cursor: isAdding ? 'not-allowed' : 'pointer',
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

    {/* Artist image or gradient placeholder */}
    {track.artist_image_url ? (
      <Box
        component="img"
        src={track.artist_image_url}
        alt={track.artist}
        sx={{
          width: 44,
          height: 44,
          borderRadius: '10px',
          objectFit: 'cover',
          flexShrink: 0,
        }}
      />
    ) : (
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
    )}

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

    {/* Play button */}
    <Box
      sx={{
        width: 36,
        height: 36,
        borderRadius: '50%',
        background: 'linear-gradient(135deg, #06B6D4, #7C3AED)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        flexShrink: 0,
        opacity: isAdding ? 0.4 : 1,
        transition: 'transform 0.15s ease',
        '&:hover': { transform: 'scale(1.1)' },
      }}
    >
      <PlayArrowIcon sx={{ fontSize: 20, color: '#FFFFFF' }} />
    </Box>
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
  sessionId,
  onTrackSelected,
  onSearchStateChange,
}) => {
  const [searchMode, setSearchMode] = useState<'title' | 'mood'>('title');
  const searchModeRef = useRef<'title' | 'mood'>('title');
  const [query, setQuery] = useState('');
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [results, setResults] = useState<TrackSearchItem[] | null>(null);
  const [resultsTotal, setResultsTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [addingTrackId, setAddingTrackId] = useState<string | null>(null);
  const [searched, setSearched] = useState(false);

  // Infinite scroll state
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);

  const debounceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const suggestGenRef = useRef(0); // generation counter — invalidates stale suggest responses
  const loadMoreRef = useRef<() => Promise<void>>(async () => {});
  const inputRef = useRef<HTMLInputElement>(null);
  const searchBarRef = useRef<HTMLDivElement>(null);
  const popperRef = useRef<HTMLDivElement>(null);
  const sentinelRef = useRef<HTMLDivElement>(null);
  const searchQueryRef = useRef('');

  // ── Helpers: kill pending suggestions ─────────────────────────────────

  const dismissSuggestions = useCallback(() => {
    if (debounceTimerRef.current !== null) {
      clearTimeout(debounceTimerRef.current);
      debounceTimerRef.current = null;
    }
    suggestGenRef.current += 1; // invalidate any in-flight API response
    setSuggestions([]);
    setShowSuggestions(false);
  }, []);

  // ── Suggestion debounce ──────────────────────────────────────────────────

  useEffect(() => {
    if (debounceTimerRef.current !== null) {
      clearTimeout(debounceTimerRef.current);
    }

    const trimmed = query.trim();

    if (!trimmed || searchMode === 'mood') {
      setSuggestions([]);
      setShowSuggestions(false);
      return;
    }

    const gen = ++suggestGenRef.current;

    debounceTimerRef.current = setTimeout(() => {
      void api.suggestTracks(trimmed).then((data) => {
        if (gen !== suggestGenRef.current) return; // stale — search or newer typing happened
        setSuggestions(data);
        setShowSuggestions(data.length > 0);
      }).catch(() => {
        if (gen !== suggestGenRef.current) return;
        setSuggestions([]);
        setShowSuggestions(false);
      });
    }, DEBOUNCE_MS);

    return () => {
      if (debounceTimerRef.current !== null) {
        clearTimeout(debounceTimerRef.current);
      }
    };
  }, [query, searchMode]);

  // ── Search ───────────────────────────────────────────────────────────────

  const runSearch = useCallback(async (q: string): Promise<void> => {
    const trimmed = q.trim();
    if (!trimmed) return;

    dismissSuggestions();
    setLoading(true);
    setSearched(true);
    onSearchStateChange?.(true);
    setOffset(0);
    searchQueryRef.current = trimmed;
    searchModeRef.current = searchMode;

    try {
      const moodMode = searchMode === 'mood';
      const data = await api.searchTracks(trimmed, moodMode ? 10 : PAGE_SIZE, 0, searchMode, moodMode ? sessionId : undefined);
      setResults(data.items);
      setResultsTotal(data.total);
      setHasMore(data.items.length < data.total);
    } catch {
      setResults([]);
      setResultsTotal(0);
      setHasMore(false);
    } finally {
      setLoading(false);
    }
  }, [dismissSuggestions, searchMode]);

  // ── Load more (infinite scroll) ─────────────────────────────────────────

  const loadMore = useCallback(async (): Promise<void> => {
    if (loadingMore || !hasMore) return;

    const currentQuery = searchQueryRef.current;
    const newOffset = offset + PAGE_SIZE;
    setLoadingMore(true);

    try {
      const moodM = searchModeRef.current === 'mood';
      const data = await api.searchTracks(currentQuery, moodM ? 10 : PAGE_SIZE, newOffset, searchModeRef.current, moodM ? sessionId : undefined);
      if (searchQueryRef.current !== currentQuery) return;

      setResults((prev) => (prev ? [...prev, ...data.items] : data.items));
      setOffset(newOffset);
      setHasMore(newOffset + data.items.length < data.total);
    } catch {
      // Don't clear results on load-more failure
    } finally {
      setLoadingMore(false);
    }
  }, [offset, hasMore, loadingMore]);

  // Keep ref in sync so the observer callback always calls the latest version.
  loadMoreRef.current = loadMore;

  // ── Scroll-based auto-load ───────────────────────────────────────────────
  // Uses capture-phase scroll listener on window — catches scroll on ANY
  // ancestor regardless of which container actually scrolls.  Checks the
  // sentinel's viewport position via getBoundingClientRect.

  useEffect(() => {
    if (!hasMore) return;

    let rafId = 0;
    const check = () => {
      const sentinel = sentinelRef.current;
      if (!sentinel) return;
      const rect = sentinel.getBoundingClientRect();
      if (rect.top < window.innerHeight + 300) {
        void loadMoreRef.current();
      }
    };

    const onScroll = () => {
      cancelAnimationFrame(rafId);
      rafId = requestAnimationFrame(check);
    };

    // capture: true catches scroll events from inner containers (scroll doesn't bubble)
    window.addEventListener('scroll', onScroll, { passive: true, capture: true });
    return () => {
      window.removeEventListener('scroll', onScroll, { capture: true } as EventListenerOptions);
      cancelAnimationFrame(rafId);
    };
  }, [hasMore]);

  // ── Handlers ────────────────────────────────────────────────────────────

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>): void => {
    if (e.key === 'Enter') {
      dismissSuggestions();
      void runSearch(query);
    }
    if (e.key === 'Escape') {
      dismissSuggestions();
    }
  };

  const handleSuggestionClick = (suggestion: string): void => {
    setQuery(suggestion);
    dismissSuggestions();
    void runSearch(suggestion);
  };

  const handleClear = (): void => {
    setQuery('');
    setSuggestions([]);
    setShowSuggestions(false);
    setResults(null);
    setSearched(false);
    setHasMore(false);
    setOffset(0);
    inputRef.current?.focus();
    onSearchStateChange?.(false);
  };

  const handleClickAway = (event: MouseEvent | TouchEvent): void => {
    if (popperRef.current?.contains(event.target as Node)) return;
    setShowSuggestions(false);
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
      {/* Mode toggle */}
      <ToggleButtonGroup
        value={searchMode}
        exclusive
        onChange={(_e, val) => {
          if (val === null) return;
          setSearchMode(val);
          setResults(null);
          setSearched(false);
          setHasMore(false);
          setOffset(0);
        }}
        size="small"
        sx={{
          alignSelf: 'flex-start',
          '& .MuiToggleButton-root': {
            color: 'rgba(255,255,255,0.4)',
            borderColor: 'rgba(255,255,255,0.12)',
            fontSize: '12px',
            fontWeight: 600,
            textTransform: 'none',
            px: 1.75,
            py: 0.6,
            '&.Mui-selected': {
              color: '#06B6D4',
              background: 'rgba(6,182,212,0.12)',
              borderColor: 'rgba(6,182,212,0.4)',
              '&:hover': { background: 'rgba(6,182,212,0.18)' },
            },
          },
        }}
      >
        <ToggleButton value="title">По названию</ToggleButton>
        <ToggleButton value="mood">По настроению</ToggleButton>
      </ToggleButtonGroup>

      {/* Search input + suggestions */}
      <ClickAwayListener onClickAway={handleClickAway}>
        <Box>
          <Box
            ref={searchBarRef}
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
                if (suggestions.length > 0 && !searched) setShowSuggestions(true);
              }}
              placeholder={searchMode === 'mood' ? 'Например: что-нибудь весёлое про лето...' : 'Исполнитель, название, текст...'}
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

          {/* Suggestions dropdown (Popper portal — not clipped by overflow) */}
          <Popper
            open={showSuggestions && suggestions.length > 0}
            anchorEl={searchBarRef.current}
            placement="bottom-start"
            style={{ width: searchBarRef.current?.clientWidth, zIndex: 1300 }}
            modifiers={[{ name: 'offset', options: { offset: [0, 6] } }]}
          >
            <Paper
              ref={popperRef}
              elevation={8}
              sx={{
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
          </Popper>
        </Box>
      </ClickAwayListener>

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

          {/* Infinite scroll sentinel + load-more button */}
          {hasMore && (
            <Box
              ref={sentinelRef}
              onClick={() => { if (!loadingMore) void loadMore(); }}
              sx={{
                display: 'flex',
                justifyContent: 'center',
                py: 2,
                cursor: loadingMore ? 'default' : 'pointer',
              }}
            >
              {loadingMore ? (
                <CircularProgress size={28} sx={{ color: 'rgba(6,182,212,0.6)' }} />
              ) : (
                <Typography
                  sx={{
                    fontSize: '13px',
                    fontWeight: 600,
                    color: 'rgba(6,182,212,0.7)',
                    '&:hover': { color: '#67E8F9' },
                  }}
                >
                  Показать ещё
                </Typography>
              )}
            </Box>
          )}
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

    </Box>
  );
};
