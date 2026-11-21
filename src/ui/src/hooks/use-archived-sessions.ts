import { useCallback, useMemo, useState } from "react";

import type { AcpSessionInfo } from "@/lib/acp/types";

const ARCHIVED_SESSIONS_STORAGE_KEY = "cbi-acp-archived-sessions";

function readStoredArchivedIds(): string[] {
  try {
    const raw = window.localStorage.getItem(ARCHIVED_SESSIONS_STORAGE_KEY);
    if (!raw) {
      return [];
    }
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      return [];
    }
    return parsed.filter(
      (item): item is string => typeof item === "string" && item.length > 0,
    );
  } catch {
    return [];
  }
}

function writeStoredArchivedIds(ids: ReadonlySet<string>): void {
  try {
    window.localStorage.setItem(
      ARCHIVED_SESSIONS_STORAGE_KEY,
      JSON.stringify(Array.from(ids)),
    );
  } catch {
    // Ignore storage quota or private-browsing write failures.
  }
}

export interface UseArchivedSessionsResult {
  archivedIds: ReadonlySet<string>;
  showArchived: boolean;
  setShowArchived: (show: boolean) => void;
  isArchived: (sessionId: string) => boolean;
  toggleArchived: (sessionId: string) => void;
  countArchivedIn: (sessions: AcpSessionInfo[]) => number;
}

export function useArchivedSessions(): UseArchivedSessionsResult {
  const [archivedList, setArchivedList] = useState<string[]>(() =>
    readStoredArchivedIds(),
  );
  const [showArchived, setShowArchived] = useState(false);

  const archivedIds = useMemo(
    () => new Set<string>(archivedList),
    [archivedList],
  );

  const isArchived = useCallback(
    (sessionId: string) => archivedIds.has(sessionId),
    [archivedIds],
  );

  const toggleArchived = useCallback((sessionId: string) => {
    setArchivedList((prev) => {
      const nextSet = new Set(prev);
      if (nextSet.has(sessionId)) {
        nextSet.delete(sessionId);
      } else {
        nextSet.add(sessionId);
      }
      writeStoredArchivedIds(nextSet);
      return Array.from(nextSet);
    });
  }, []);

  const countArchivedIn = useCallback(
    (sessions: AcpSessionInfo[]) =>
      sessions.reduce(
        (acc, item) => (archivedIds.has(item.sessionId) ? acc + 1 : acc),
        0,
      ),
    [archivedIds],
  );

  return {
    archivedIds,
    showArchived,
    setShowArchived,
    isArchived,
    toggleArchived,
    countArchivedIn,
  };
}
