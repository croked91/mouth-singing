import React, { useEffect, useRef, useState, useCallback } from 'react';
import { Box, Typography } from '@mui/material';
import type { SyllableTiming } from '../types';

// ─── Types ────────────────────────────────────────────────────────────────────

interface LyricLine {
  syllables: SyllableTiming[];
  startTime: number;
  endTime: number;
}

interface LyricHighlightProps {
  syllableTimings: SyllableTiming[];
  getCurrentTime: () => number;
  isPlaying: boolean;
}

// ─── Constants ────────────────────────────────────────────────────────────────

const LINE_GAP_THRESHOLD_SEC = 1.0;

const STYLE_PREV_LINE = {
  fontWeight: 500,
  fontSize: '36px',
  color: 'rgba(6,182,212,0.5)',
  filter: 'blur(1px)',
  lineHeight: 1.3,
  transition: 'opacity 0.3s ease',
  userSelect: 'none' as const,
  whiteSpace: 'pre-wrap' as const,
} as const;

const STYLE_NEXT_LINE = {
  fontWeight: 500,
  fontSize: '36px',
  color: 'rgba(255,255,255,0.45)',
  lineHeight: 1.3,
  transition: 'opacity 0.3s ease',
  userSelect: 'none' as const,
  whiteSpace: 'pre-wrap' as const,
} as const;

const ACTIVE_LINE_FONT_SIZE = '72px';

// ─── Helper: group syllables into lines ───────────────────────────────────────

function groupIntoLines(syllables: SyllableTiming[]): LyricLine[] {
  if (syllables.length === 0) return [];

  const lines: LyricLine[] = [];
  let currentGroup: SyllableTiming[] = [syllables[0]];

  for (let i = 1; i < syllables.length; i++) {
    const gap = syllables[i].start - syllables[i - 1].end;
    if (gap > LINE_GAP_THRESHOLD_SEC) {
      lines.push({
        syllables: currentGroup,
        startTime: currentGroup[0].start,
        endTime: currentGroup[currentGroup.length - 1].end,
      });
      currentGroup = [syllables[i]];
    } else {
      currentGroup.push(syllables[i]);
    }
  }

  if (currentGroup.length > 0) {
    lines.push({
      syllables: currentGroup,
      startTime: currentGroup[0].start,
      endTime: currentGroup[currentGroup.length - 1].end,
    });
  }

  return lines;
}

// ─── Helper: find active line index for a given time ─────────────────────────

function findActiveLineIndex(lines: LyricLine[], currentTime: number): number {
  // Find the last line whose start <= currentTime
  let active = -1;
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].startTime <= currentTime) {
      active = i;
    } else {
      break;
    }
  }
  // If we haven't reached the first line yet, show line 0 as active
  if (active === -1 && lines.length > 0) {
    active = 0;
  }
  return active;
}

// ─── Helper: compute line progress (0-1) ─────────────────────────────────────

function computeLineProgress(line: LyricLine, currentTime: number): number {
  const duration = line.endTime - line.startTime;
  if (duration <= 0) return 1;
  const elapsed = Math.max(0, currentTime - line.startTime);
  return Math.min(1, elapsed / duration);
}

// ─── ActiveLineSyllables — rendered without React state churn ─────────────────

interface ActiveLineProps {
  line: LyricLine;
  getCurrentTime: () => number;
  isPlaying: boolean;
}

const ActiveLine: React.FC<ActiveLineProps> = ({ line, getCurrentTime, isPlaying }) => {
  // We use a ref-driven rAF loop that directly mutates DOM spans to avoid
  // per-syllable React re-renders at 60fps.
  const containerRef = useRef<HTMLSpanElement>(null);
  const progressBarRef = useRef<HTMLDivElement>(null);
  const rafRef = useRef<number | null>(null);

  const update = useCallback(() => {
    const currentTime = getCurrentTime();
    const container = containerRef.current;
    const progressBar = progressBarRef.current;
    if (!container) return;

    const spans = container.querySelectorAll<HTMLSpanElement>('[data-syllable-idx]');
    spans.forEach((span) => {
      const idx = parseInt(span.dataset.syllableIdx ?? '0', 10);
      const syl = line.syllables[idx];
      if (!syl) return;

      if (syl.end <= currentTime) {
        // Sung
        span.style.color = 'rgba(255,255,255,0.3)';
        span.style.fontWeight = '800';
        span.style.textShadow = 'none';
      } else if (syl.start <= currentTime) {
        // Active
        span.style.color = '#F0ABFC';
        span.style.fontWeight = '900';
        span.style.textShadow =
          '0 0 20px rgba(240,171,252,0.9), 0 0 40px rgba(167,85,247,0.6), 0 0 80px rgba(124,58,237,0.4)';
      } else {
        // Upcoming
        span.style.color = 'rgba(255,255,255,0.9)';
        span.style.fontWeight = '800';
        span.style.textShadow = '0 0 8px rgba(255,255,255,0.15)';
      }
    });

    // Update progress bar
    if (progressBar) {
      const progress = computeLineProgress(line, currentTime);
      progressBar.style.width = `${progress * 100}%`;
    }

    if (isPlaying) {
      rafRef.current = requestAnimationFrame(update);
    }
  }, [line, getCurrentTime, isPlaying]);

  useEffect(() => {
    // Run immediately to paint current state (even when paused)
    update();

    if (isPlaying) {
      rafRef.current = requestAnimationFrame(update);
    }

    return () => {
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
    };
  }, [update, isPlaying]);

  // Re-run update once on pause so final state is painted correctly
  useEffect(() => {
    if (!isPlaying) {
      update();
    }
  }, [isPlaying, update]);

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '10px' }}>
      {/* Syllable spans */}
      <Box
        component="span"
        ref={containerRef}
        sx={{
          fontSize: ACTIVE_LINE_FONT_SIZE,
          lineHeight: 1.15,
          fontFamily: '"Inter", sans-serif',
          display: 'block',
          textAlign: 'center',
          whiteSpace: 'pre-wrap',
          userSelect: 'none',
        }}
      >
        {line.syllables.map((syl, idx) => (
          <Box
            key={idx}
            component="span"
            data-syllable-idx={idx}
            sx={{
              color: 'rgba(255,255,255,0.9)',
              fontWeight: 800,
              textShadow: '0 0 8px rgba(255,255,255,0.15)',
              transition: 'none',
              display: 'inline',
            }}
          >
            {syl.syllable}
          </Box>
        ))}
      </Box>

      {/* Progress bar */}
      <Box
        sx={{
          width: '100%',
          maxWidth: '960px',
          height: '3px',
          borderRadius: '2px',
          backgroundColor: 'rgba(255,255,255,0.08)',
          overflow: 'hidden',
          position: 'relative',
        }}
      >
        <Box
          ref={progressBarRef}
          sx={{
            height: '100%',
            width: '0%',
            background: 'linear-gradient(90deg, #F0ABFC, #7C3AED)',
            boxShadow: '0 0 8px rgba(240,171,252,0.6), 0 0 16px rgba(124,58,237,0.4)',
            borderRadius: '2px',
            transition: 'none',
          }}
        />
      </Box>
    </Box>
  );
};

// ─── Main LyricHighlight component ───────────────────────────────────────────

export const LyricHighlight: React.FC<LyricHighlightProps> = ({
  syllableTimings,
  getCurrentTime,
  isPlaying,
}) => {
  const lines = useRef<LyricLine[]>(groupIntoLines(syllableTimings)).current;

  // Active line index is React state because transitioning lines needs a
  // re-render to swap out components. We keep it coarse (one update per line
  // transition, not per frame).
  const [activeLineIndex, setActiveLineIndex] = useState<number>(() =>
    findActiveLineIndex(lines, getCurrentTime())
  );

  const rafRef = useRef<number | null>(null);
  const lastActiveRef = useRef<number>(activeLineIndex);

  const checkLineTransition = useCallback(() => {
    const currentTime = getCurrentTime();
    const idx = findActiveLineIndex(lines, currentTime);
    if (idx !== lastActiveRef.current) {
      lastActiveRef.current = idx;
      setActiveLineIndex(idx);
    }
    if (isPlaying) {
      rafRef.current = requestAnimationFrame(checkLineTransition);
    }
  }, [lines, getCurrentTime, isPlaying]);

  useEffect(() => {
    // Sync immediately on mount or when isPlaying changes
    checkLineTransition();

    if (isPlaying) {
      rafRef.current = requestAnimationFrame(checkLineTransition);
    }

    return () => {
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
    };
  }, [checkLineTransition, isPlaying]);

  // When paused, still sync line index so seeking works correctly
  useEffect(() => {
    if (!isPlaying) {
      const idx = findActiveLineIndex(lines, getCurrentTime());
      if (idx !== lastActiveRef.current) {
        lastActiveRef.current = idx;
        setActiveLineIndex(idx);
      }
    }
  });

  if (lines.length === 0) {
    return (
      <Box
        sx={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          flex: 1,
          height: '100%',
        }}
      >
        <Typography
          sx={{
            fontSize: '28px',
            fontWeight: 500,
            color: 'rgba(255,255,255,0.3)',
            letterSpacing: '0.04em',
          }}
        >
          Субтитры недоступны
        </Typography>
      </Box>
    );
  }

  const prevLine = activeLineIndex > 0 ? lines[activeLineIndex - 1] : null;
  const activeLine = lines[activeLineIndex] ?? null;
  const nextLine =
    activeLineIndex < lines.length - 1 ? lines[activeLineIndex + 1] : null;

  return (
    <Box
      sx={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        flex: 1,
        height: '100%',
        gap: '40px',
        px: '120px',
        overflow: 'hidden',
      }}
    >
      {/* Previous line */}
      <Box
        sx={{
          minHeight: '52px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          opacity: prevLine ? 1 : 0,
          transition: 'opacity 0.4s ease',
        }}
      >
        {prevLine && (
          <Box component="span" sx={STYLE_PREV_LINE}>
            {prevLine.syllables.map((s) => s.syllable).join('')}
          </Box>
        )}
      </Box>

      {/* Active line */}
      <Box
        sx={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          width: '100%',
          minHeight: '120px',
        }}
      >
        {activeLine && (
          <ActiveLine
            key={activeLineIndex}
            line={activeLine}
            getCurrentTime={getCurrentTime}
            isPlaying={isPlaying}
          />
        )}
      </Box>

      {/* Next line */}
      <Box
        sx={{
          minHeight: '52px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          opacity: nextLine ? 1 : 0,
          transition: 'opacity 0.4s ease',
        }}
      >
        {nextLine && (
          <Box component="span" sx={STYLE_NEXT_LINE}>
            {nextLine.syllables.map((s) => s.syllable).join('')}
          </Box>
        )}
      </Box>
    </Box>
  );
};
