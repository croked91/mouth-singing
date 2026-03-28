import React, { useEffect, useLayoutEffect, useRef, useState, useCallback } from 'react';
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
const MIN_LINE_CHARS_FOR_PUNCT_BREAK = 20;
const MAX_LINE_CHARS = 55;

const ACTIVE_LINE_FONT_SIZE = '72px';
const STYLE_TRANSITION = 'opacity 0.5s ease, filter 0.5s ease';
const LINE_TRANSITION_MS = 600;


// ─── Helper: group syllables into lines ───────────────────────────────────────

function groupIntoLines(syllables: SyllableTiming[]): LyricLine[] {
  if (syllables.length === 0) return [];

  const pushLine = (group: SyllableTiming[]) => ({
    syllables: group,
    startTime: group[0].start,
    endTime: group[group.length - 1].end,
  });

  const endsWithSentencePunct = (text: string): boolean =>
    /[.!?]$/.test(text.trimEnd());

  const isPunctOnly = (text: string): boolean =>
    /^[\s.!?,;:…"'«»„""—–\-]+$/.test(text);

  const lines: LyricLine[] = [];
  let currentGroup: SyllableTiming[] = [syllables[0]];
  let currentChars = syllables[0].syllable.length;

  for (let i = 1; i < syllables.length; i++) {
    const gap = syllables[i].start - syllables[i - 1].end;
    const syllableText = syllables[i].syllable;
    const syllableLen = syllableText.length;
    const isWordBoundary = syllableText.startsWith(' ');
    const prevText = syllables[i - 1].syllable;

    // Explicit line break marker from backend (LRC line boundaries).
    // Strip the \n prefix and force a new line group.
    if (syllableText.startsWith('\n')) {
      lines.push(pushLine(currentGroup));
      const stripped = { ...syllables[i], syllable: syllableText.slice(1) };
      currentGroup = [stripped];
      currentChars = stripped.syllable.length;
      continue;
    }

    // Never start a new line with punctuation-only token
    if (isPunctOnly(syllableText)) {
      currentGroup.push(syllables[i]);
      currentChars += syllableLen;
      continue;
    }

    // Always break on a natural pause
    if (gap > LINE_GAP_THRESHOLD_SEC) {
      lines.push(pushLine(currentGroup));
      currentGroup = [syllables[i]];
      currentChars = syllableLen;
      continue;
    }

    // Break after sentence punctuation (.!?) when line is long enough
    if (currentChars >= MIN_LINE_CHARS_FOR_PUNCT_BREAK && endsWithSentencePunct(prevText)) {
      lines.push(pushLine(currentGroup));
      currentGroup = [syllables[i]];
      currentChars = syllableLen;
      continue;
    }

    // Fallback: break at word boundary when line is too long
    if (currentChars + syllableLen > MAX_LINE_CHARS && isWordBoundary && currentChars > 0) {
      lines.push(pushLine(currentGroup));
      currentGroup = [syllables[i]];
      currentChars = syllableLen;
      continue;
    }

    currentGroup.push(syllables[i]);
    currentChars += syllableLen;
  }

  if (currentGroup.length > 0) {
    lines.push(pushLine(currentGroup));
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
  const progressTrackRef = useRef<HTMLDivElement>(null);
  const rafRef = useRef<number | null>(null);

  const update = useCallback(() => {
    const currentTime = getCurrentTime();
    const container = containerRef.current;
    const progressBar = progressBarRef.current;
    const progressTrack = progressTrackRef.current;
    if (!container) return;

    const spans = container.querySelectorAll<HTMLSpanElement>('[data-syllable-idx]');
    let fillRatio = 0;

    spans.forEach((span) => {
      const idx = parseInt(span.dataset.syllableIdx ?? '0', 10);
      const syl = line.syllables[idx];
      if (!syl) return;

      if (syl.end <= currentTime) {
        // Sung — bright
        span.style.color = 'rgba(255,255,255,0.85)';
        span.style.textShadow = 'none';
      } else if (syl.start <= currentTime) {
        // Active — highlighted
        span.style.color = '#C4B5FD';
        span.style.textShadow = '0 0 16px rgba(196,181,253,0.5)';
      } else {
        // Upcoming — dim
        span.style.color = 'rgba(255,255,255,0.35)';
        span.style.textShadow = 'none';
      }
    });

    // Compute progress bar fill from syllable span positions
    if (progressBar && progressTrack && spans.length > 0) {
      const containerRect = container.getBoundingClientRect();
      const containerWidth = containerRect.width;

      if (containerWidth > 0) {
        // Find the rightmost edge of sung/active syllables
        let maxRight = 0;
        let hasProgress = false;

        spans.forEach((span) => {
          const idx = parseInt(span.dataset.syllableIdx ?? '0', 10);
          const syl = line.syllables[idx];
          if (!syl) return;

          if (syl.end <= currentTime) {
            // Fully sung — count full span width
            const spanRect = span.getBoundingClientRect();
            const right = spanRect.right - containerRect.left;
            if (right > maxRight) maxRight = right;
            hasProgress = true;
          } else if (syl.start <= currentTime) {
            // Partially active — interpolate within this syllable
            const sylDuration = syl.end - syl.start;
            const elapsed = currentTime - syl.start;
            const sylProgress = sylDuration > 0 ? Math.min(1, elapsed / sylDuration) : 1;
            const spanRect = span.getBoundingClientRect();
            const spanLeft = spanRect.left - containerRect.left;
            const spanWidth = spanRect.width;
            const right = spanLeft + spanWidth * sylProgress;
            if (right > maxRight) maxRight = right;
            hasProgress = true;
          }
        });

        fillRatio = hasProgress ? maxRight / containerWidth : 0;
      }

      // Size the track to match text width, fill the bar
      progressTrack.style.width = `${containerWidth}px`;
      progressBar.style.width = `${fillRatio * 100}%`;
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
    <Box sx={{ position: 'relative' }}>
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
              color: 'rgba(255,255,255,0.35)',
              transition: 'none',
              display: 'inline',
            }}
          >
            {syl.syllable}
          </Box>
        ))}
      </Box>

      {/* Progress bar — absolutely positioned, no layout impact */}
      <Box
        ref={progressTrackRef}
        sx={{
          position: 'absolute',
          bottom: '-10px',
          left: '50%',
          transform: 'translateX(-50%)',
          height: '3px',
          borderRadius: '2px',
          backgroundColor: 'rgba(255,255,255,0.08)',
          overflow: 'hidden',
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

  // Refs for measuring line positions and computing transform offset
  const lineRefs = useRef<(HTMLDivElement | null)[]>([]);
  const containerRef = useRef<HTMLDivElement>(null);
  const innerRef = useRef<HTMLDivElement>(null);
  const [translateY, setTranslateY] = useState(0);

  // Compute the translateY offset to center the active line.
  // useLayoutEffect runs synchronously before paint — no flash of wrong position.
  useLayoutEffect(() => {
    const container = containerRef.current;
    const activeLine = lineRefs.current[activeLineIndex];
    if (!container || !activeLine) return;

    const containerHeight = container.clientHeight;
    const lineTop = activeLine.offsetTop;
    const lineHeight = activeLine.offsetHeight;

    // Center the active line vertically in the container
    const offset = lineTop - containerHeight / 2 + lineHeight / 2;
    setTranslateY(-offset);
  }, [activeLineIndex]);

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

  const lineStyle = {
    fontSize: ACTIVE_LINE_FONT_SIZE,
    lineHeight: 1.15,
    fontFamily: '"Inter", sans-serif',
    display: 'block',
    textAlign: 'center' as const,
    whiteSpace: 'pre-wrap' as const,
    userSelect: 'none' as const,
    fontWeight: 500,
  };

  return (
    <Box
      ref={containerRef}
      sx={{
        flex: 1,
        height: '100%',
        overflow: 'hidden',
        px: '120px',
        position: 'relative',
      }}
    >
      <Box
        ref={innerRef}
        sx={{
          transform: `translateY(${translateY}px)`,
          transition: `transform ${LINE_TRANSITION_MS}ms cubic-bezier(0.25, 0.1, 0.25, 1)`,
          willChange: 'transform',
        }}
      >
        {lines.map((line, i) => {
          const dist = i - activeLineIndex;
          const isActive = dist === 0;
          const isPrev = dist === -1;
          const isNext = dist === 1;

          return (
            <Box
              key={i}
              ref={(el: HTMLDivElement | null) => { lineRefs.current[i] = el; }}
              sx={{
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                py: '14px',
                transition: STYLE_TRANSITION,
                opacity: isActive ? 1 : (isPrev || isNext) ? 1 : 0,
                filter: isPrev ? 'blur(1px)' : 'none',
              }}
            >
              {isActive ? (
                <ActiveLine
                  line={line}
                  getCurrentTime={getCurrentTime}
                  isPlaying={isPlaying}
                />
              ) : (
                <Box
                  component="span"
                  sx={{
                    ...lineStyle,
                    color: isPrev ? 'rgba(6,182,212,0.5)' : 'rgba(255,255,255,0.45)',
                  }}
                >
                  {line.syllables.map((s) => s.syllable).join('')}
                </Box>
              )}
            </Box>
          );
        })}
      </Box>
    </Box>
  );
};
