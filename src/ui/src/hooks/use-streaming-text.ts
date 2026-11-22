import * as React from "react";

const BASE_CHARS_PER_SEC = 120;
const BACKLOG_RATE_FACTOR = 3.2;
const MAX_CHARS_PER_SEC = 2400;

export interface StreamingTextState {
  displayedText: string;
  isStreaming: boolean;
}

export function useStreamingText(
  targetText: string,
  enabled: boolean,
): StreamingTextState {
  const [revealedCount, setRevealedCount] = React.useState<number>(() =>
    enabled ? 0 : targetText.length,
  );
  const posRef = React.useRef<number>(enabled ? 0 : targetText.length);
  const targetLen = targetText.length;

  React.useEffect(() => {
    if (!enabled) {
      posRef.current = targetLen;
      return;
    }

    let cancelled = false;
    let lastFrameTs = performance.now();
    let carry = 0;
    let rafId = 0;

    const tick = (now: number) => {
      if (cancelled) {
        return;
      }
      const reducedMotion =
        typeof window.matchMedia === "function" &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      if (reducedMotion) {
        posRef.current = targetLen;
        setRevealedCount(targetLen);
        return;
      }

      const current = Math.min(posRef.current, targetLen);
      if (current >= targetLen) {
        if (posRef.current !== targetLen) {
          posRef.current = targetLen;
          setRevealedCount(targetLen);
        }
        return;
      }

      const dtSec = Math.min(0.05, Math.max(0.001, (now - lastFrameTs) / 1000));
      lastFrameTs = now;
      const backlog = targetLen - current;
      const charsPerSec = Math.min(
        MAX_CHARS_PER_SEC,
        BASE_CHARS_PER_SEC + backlog * BACKLOG_RATE_FACTOR,
      );
      carry += charsPerSec * dtSec;
      const delta = Math.max(1, Math.floor(carry));
      carry = Math.max(0, carry - delta);
      const next = Math.min(targetLen, current + delta);
      posRef.current = next;
      setRevealedCount(next);

      if (next < targetLen && !cancelled) {
        rafId = window.requestAnimationFrame(tick);
      }
    };

    rafId = window.requestAnimationFrame(tick);
    return () => {
      cancelled = true;
      window.cancelAnimationFrame(rafId);
    };
  }, [enabled, targetLen]);

  if (!enabled) {
    return { displayedText: targetText, isStreaming: false };
  }

  const safeCount = Math.min(revealedCount, targetLen);
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
      fallbackStartRef.current = Date.now();
    }
    const sample = () => {
      const origin = startedAtMs ?? fallbackStartRef.current ?? Date.now();
      const elapsed = Math.max(0.1, (Date.now() - origin) / 1000);
      setLiveSec(elapsed);
    };
    const rafId = window.requestAnimationFrame(sample);
    const timerId = window.setInterval(sample, 100);
    return () => {
      window.cancelAnimationFrame(rafId);
      window.clearInterval(timerId);
    };
  }, [active, startedAtMs]);

  if (active) {
    return liveSec ?? 0.1;
  }
  if (settledSec !== undefined && settledSec > 0) {
    return settledSec;
  }
  return liveSec;
}
