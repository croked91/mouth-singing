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

// ─── Helper: measure text width via offscreen canvas ─────────────────────────

let _measureCanvas: HTMLCanvasElement | null = null;
function measureTextWidth(text: string, fontSizePx: number): number {
  if (!_measureCanvas) _measureCanvas = document.createElement('canvas');
  const ctx = _measureCanvas.getContext('2d')!;
  ctx.font = `500 ${fontSizePx}px "Inter", sans-serif`;
  return ctx.measureText(text).width;
}

const BASE_FONT_SIZE = 72;
const MIN_FONT_SIZE = 28;
const STYLE_TRANSITION = 'opacity 0.5s ease, filter 0.5s ease';
const LINE_TRANSITION_MS = 600;


// ─── Helper: group syllables into lines ───────────────────────────────────────

function groupIntoLines(syllables: SyllableTiming[]): LyricLine[] {
  if (syllables.length === 0) return [];

  const makeLine = (group: SyllableTiming[]): LyricLine => ({
    syllables: group,
    startTime: group[0].start,
    endTime: group[group.length - 1].end,
  });

  const lines: LyricLine[] = [];
  let currentGroup: SyllableTiming[] = [syllables[0]];

  for (let i = 1; i < syllables.length; i++) {
    const syllableText = syllables[i].syllable;

    // Line break marker from backend (\n at the start of a syllable).
    // Strip the \n prefix and start a new line.
    if (syllableText.startsWith('\n')) {
      lines.push(makeLine(currentGroup));
      currentGroup = [{ ...syllables[i], syllable: syllableText.slice(1) }];
      continue;
    }

    currentGroup.push(syllables[i]);
  }

  if (currentGroup.length > 0) {
    lines.push(makeLine(currentGroup));
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
  // Advance to next line once current line's syllables are all sung,
  // so the scroll animation happens during the silence gap between lines.
  if (
    active >= 0 &&
    active < lines.length - 1 &&
    currentTime > lines[active].endTime
  ) {
    active += 1;
  }
  return active;
}

// ─── ActiveLineSyllables — rendered without React state churn ─────────────────

interface ActiveLineProps {
  line: LyricLine;
  getCurrentTime: () => number;
  isPlaying: boolean;
  fontSize: number;
}

const ActiveLine: React.FC<ActiveLineProps> = ({ line, getCurrentTime, isPlaying, fontSize }) => {
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

    const SUNG_COLOR = 'rgba(255,255,255,0.85)';
    const ACTIVE_COLOR = '#C4B5FD';
    const DIM_COLOR = 'rgba(255,255,255,0.35)';

    spans.forEach((span) => {
      const idx = parseInt(span.dataset.syllableIdx ?? '0', 10);
      const syl = line.syllables[idx];
      if (!syl) return;

      if (syl.end <= currentTime) {
        // Sung — fully bright
        span.style.background = 'none';
        span.style.backgroundClip = '';
        span.style.webkitTextFillColor = '';
        span.style.color = SUNG_COLOR;
        span.style.textShadow = 'none';
      } else if (syl.start <= currentTime) {
        // Active — gradient sweep across the syllable
        const sylDuration = syl.end - syl.start;
        const elapsed = currentTime - syl.start;
        const pct = sylDuration > 0 ? Math.min(1, elapsed / sylDuration) * 100 : 100;
        span.style.background = `linear-gradient(90deg, ${ACTIVE_COLOR} ${pct}%, ${DIM_COLOR} ${pct}%)`;
        span.style.backgroundClip = 'text';
        span.style.webkitTextFillColor = 'transparent';
        span.style.textShadow = 'none';
      } else {
        // Upcoming — dim
        span.style.background = 'none';
        span.style.backgroundClip = '';
        span.style.webkitTextFillColor = '';
        span.style.color = DIM_COLOR;
        span.style.textShadow = 'none';
      }
    });

    // Compute progress bar fill from syllable span positions
    if (progressBar && progressTrack && spans.length > 0) {
      const containerRect = container.getBoundingClientRect();
      const containerWidth = containerRect.width;

      if (containerWidth > 0) {
        let maxRight = 0;
        let hasProgress = false;

        spans.forEach((span) => {
          const idx = parseInt(span.dataset.syllableIdx ?? '0', 10);
          const syl = line.syllables[idx];
          if (!syl) return;

          if (syl.end <= currentTime) {
            // Fully sung — full span width
            const spanRect = span.getBoundingClientRect();
            const right = spanRect.right - containerRect.left;
            if (right > maxRight) maxRight = right;
            hasProgress = true;
          } else if (syl.start <= currentTime) {
            // Active — interpolate within syllable
            const sylDuration = syl.end - syl.start;
            const elapsed = currentTime - syl.start;
            const sylProgress = sylDuration > 0 ? Math.min(1, elapsed / sylDuration) : 1;
            const spanRect = span.getBoundingClientRect();
            const spanLeft = spanRect.left - containerRect.left;
            const right = spanLeft + spanRect.width * sylProgress;
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
          fontSize: `${fontSize}px`,
          lineHeight: 1.15,
          fontFamily: '"Inter", sans-serif',
          display: 'block',
          textAlign: 'center',
          whiteSpace: 'nowrap',
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

// ─── StaticLine — non-active line ────────────────────────────────────────────

interface StaticLineProps {
  text: string;
  color: string;
  lineStyle: React.CSSProperties;
}

const StaticLine: React.FC<StaticLineProps> = ({ text, color, lineStyle }) => (
  <Box component="span" sx={{ ...lineStyle, color }}>
    {text}
  </Box>
);

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
  const [fittedFontSize, setFittedFontSize] = useState(BASE_FONT_SIZE);

  // Compute a single font size that fits the longest line within the container.
  // Uses canvas measureText — no DOM measurement needed.
  const computeFittedFontSize = useCallback(() => {
    const container = containerRef.current;
    if (!container || lines.length === 0) return;

    // available width = container width minus horizontal padding (120px each side)
    const available = container.clientWidth;
    if (available <= 0) return;

    // Find the widest line at base font size
    let maxWidth = 0;
    for (const line of lines) {
      const text = line.syllables.map((s) => s.syllable).join('');
      const w = measureTextWidth(text, BASE_FONT_SIZE);
      if (w > maxWidth) maxWidth = w;
    }

    if (maxWidth <= available) {
      setFittedFontSize(BASE_FONT_SIZE);
    } else {
      const scaled = Math.floor(BASE_FONT_SIZE * (available / maxWidth));
      setFittedFontSize(Math.max(MIN_FONT_SIZE, scaled));
    }
  }, [lines]);

  // Recompute on mount + container resize
  useLayoutEffect(() => {
    computeFittedFontSize();

    const container = containerRef.current;
    if (!container) return;

    const ro = new ResizeObserver(() => computeFittedFontSize());
    ro.observe(container);
    return () => ro.disconnect();
  }, [computeFittedFontSize]);

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
  }, [activeLineIndex, fittedFontSize]);

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
    fontSize: `${fittedFontSize}px`,
    lineHeight: 1.15,
    fontFamily: '"Inter", sans-serif',
    display: 'block',
    textAlign: 'center' as const,
    whiteSpace: 'nowrap' as const,
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
                  fontSize={fittedFontSize}
                />
              ) : (
                <StaticLine
                  text={line.syllables.map((s) => s.syllable).join('')}
                  color={isPrev ? 'rgba(6,182,212,0.5)' : 'rgba(255,255,255,0.45)'}
                  lineStyle={lineStyle}
                />
              )}
            </Box>
          );
        })}
      </Box>
    </Box>
  );
};
