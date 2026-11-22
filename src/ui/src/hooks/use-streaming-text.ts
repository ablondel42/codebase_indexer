import * as React from "react";

const BASE_CHARS_PER_SEC = 180;
const BACKLOG_RATE_FACTOR = 4.5;
const MAX_CHARS_PER_SEC = 3200;

export interface StreamingTextState {
  displayedText: string;
  isStreaming: boolean;
}

function clampSurrogateBoundary(text: string, index: number): number {
  const clamped = Math.max(0, Math.min(text.length, index));
  if (clamped > 0 && clamped < text.length) {
    const code = text.charCodeAt(clamped - 1);
    if (code >= 0xd800 && code <= 0xdbff) {
      return Math.min(text.length, clamped + 1);
    }
  }
  return clamped;
}

export function useStreamingText(
  targetText: string,
  enabled: boolean,
): StreamingTextState {
  const [revealedCount, setRevealedCount] = React.useState<number>(() =>
    enabled ? 0 : targetText.length,
  );
  const posRef = React.useRef<number>(enabled ? 0 : targetText.length);
  const lastSyncedLenRef = React.useRef<number>(
    enabled ? 0 : targetText.length,
  );
  const targetTextRef = React.useRef<string>(targetText);
  const carryRef = React.useRef<number>(0);
  const lastFrameTsRef = React.useRef<number>(0);
  const rafIdRef = React.useRef<number | null>(null);
  const targetLen = targetText.length;

  React.useEffect(() => {
    return () => {
      if (rafIdRef.current !== null) {
        window.cancelAnimationFrame(rafIdRef.current);
        rafIdRef.current = null;
      }
    };
  }, []);

  React.useEffect(() => {
    targetTextRef.current = targetText;

    if (!enabled) {
      if (rafIdRef.current !== null) {
        window.cancelAnimationFrame(rafIdRef.current);
        rafIdRef.current = null;
      }
      carryRef.current = 0;
      posRef.current = targetText.length;
      if (lastSyncedLenRef.current !== targetText.length) {
        rafIdRef.current = window.requestAnimationFrame(() => {
          rafIdRef.current = null;
          const len = targetTextRef.current.length;
          posRef.current = len;
          lastSyncedLenRef.current = len;
          setRevealedCount(len);
        });
      }
      return;
    }

    if (posRef.current > targetText.length) {
      carryRef.current = 0;
      posRef.current = targetText.length;
      lastSyncedLenRef.current = targetText.length;
      if (rafIdRef.current === null) {
        rafIdRef.current = window.requestAnimationFrame(() => {
          rafIdRef.current = null;
          const len = targetTextRef.current.length;
          posRef.current = len;
          lastSyncedLenRef.current = len;
          setRevealedCount(len);
        });
      }
      return;
    }

    if (posRef.current >= targetText.length) {
      return;
    }

    // Keep any active animation frame loop uninterrupted across incoming chunks.
    if (rafIdRef.current !== null) {
      return;
    }

    const tick = (now: number) => {
      rafIdRef.current = null;
      const text = targetTextRef.current;
      const len = text.length;
      const reducedMotion =
        typeof window.matchMedia === "function" &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      if (reducedMotion) {
        posRef.current = len;
        carryRef.current = 0;
        lastSyncedLenRef.current = len;
        setRevealedCount(len);
        return;
      }

      const current = Math.min(posRef.current, len);
      if (current >= len) {
        if (posRef.current !== len || lastSyncedLenRef.current !== len) {
          posRef.current = len;
          lastSyncedLenRef.current = len;
          setRevealedCount(len);
        }
        carryRef.current = 0;
        return;
      }

      const dtSec = Math.min(
        0.05,
        Math.max(0.001, (now - lastFrameTsRef.current) / 1000),
      );
      lastFrameTsRef.current = now;
      const backlog = len - current;
      const charsPerSec = Math.min(
        MAX_CHARS_PER_SEC,
        BASE_CHARS_PER_SEC + backlog * BACKLOG_RATE_FACTOR,
      );
      carryRef.current += charsPerSec * dtSec;
      const delta = Math.floor(carryRef.current);
      if (delta >= 1) {
        carryRef.current = Math.max(0, carryRef.current - delta);
        const next = clampSurrogateBoundary(text, Math.min(len, current + delta));
        posRef.current = next;
        lastSyncedLenRef.current = next;
        setRevealedCount(next);
      }

      if (posRef.current < targetTextRef.current.length) {
        rafIdRef.current = window.requestAnimationFrame(tick);
      } else {
        carryRef.current = 0;
      }
    };

    lastFrameTsRef.current = performance.now();
    rafIdRef.current = window.requestAnimationFrame(tick);
  }, [enabled, targetText]);

  if (!enabled) {
    return { displayedText: targetText, isStreaming: false };
  }

  const safeCount = clampSurrogateBoundary(targetText, revealedCount);
  return {
    displayedText:
      safeCount >= targetLen ? targetText : targetText.slice(0, safeCount),
    isStreaming: safeCount < targetLen,
  };
}

export function useLiveElapsedSeconds(
  active: boolean,
  startedAtMs: number | null,
  settledSec?: number,
): number | undefined {
  const [liveSec, setLiveSec] = React.useState<number | undefined>(undefined);
  const fallbackStartRef = React.useRef<number | null>(null);

  React.useEffect(() => {
    if (!active) {
      fallbackStartRef.current = null;
      return;
    }
    if (startedAtMs === null && fallbackStartRef.current === null) {
      const initialOffsetMs =
        settledSec !== undefined && settledSec > 0
          ? Math.max(0, Math.round(settledSec)) * 1000
          : 0;
      fallbackStartRef.current = Date.now() - initialOffsetMs;
    }
    const sample = () => {
      const origin = startedAtMs ?? fallbackStartRef.current ?? Date.now();
      const elapsed = Math.max(0, Math.floor((Date.now() - origin) / 1000));
      setLiveSec(elapsed);
    };
    const rafId = window.requestAnimationFrame(sample);
    const timerId = window.setInterval(sample, 250);
    return () => {
      window.cancelAnimationFrame(rafId);
      window.clearInterval(timerId);
    };
  }, [active, settledSec, startedAtMs]);

  if (active) {
    return liveSec ?? 0;
  }
  if (settledSec !== undefined && settledSec > 0) {
    return Math.max(1, Math.round(settledSec));
  }
  return liveSec !== undefined && liveSec > 0
    ? Math.max(1, Math.round(liveSec))
    : liveSec;
}
