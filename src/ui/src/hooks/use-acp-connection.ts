import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  AcpBrowserClient,
  AcpRpcError,
  deriveSessionCapabilities,
  fetchServerDefaults,
  fetchSessionArtifactContent,
  fetchSessionArtifacts,
} from "@/lib/acp/client";
import {
  sanitizeAgentText,
  sanitizeMultilineAgentText,
} from "@/lib/acp/security";
import {
  ACP_AUTH_REQUIRED_CODE,
  DEFAULT_AUTH_METHOD_ID,
  type AcpActiveSessionMode,
  type AcpAuthenticateOptions,
  type AcpAuthStatus,
  type AcpConnectionStatus,
  type AcpInitializeResult,
  type AcpPlanStep,
  type AcpReplayedMessage,
  type AcpSessionArtifactContent,
  type AcpSessionArtifactSummary,
  type AcpSessionCapabilitiesFlags,
  type AcpSessionInfo,
  type AcpToolStatus,
} from "@/lib/acp/types";

export interface UseAcpConnectionState {
  status: AcpConnectionStatus;
  result: AcpInitializeResult | null;
  authStatus: AcpAuthStatus;
  activeMethodId: string | null;
  authenticatingMethodId: string | null;
  canLogout: boolean;
  defaultCwd: string;
  agyAppDataDir: string;
  agyProfileDir: string;
  includeCbiMcp: boolean;
  setIncludeCbiMcp: (enabled: boolean) => void;
  sessions: AcpSessionInfo[];
  nextCursor: string | null;
  activeSessionId: string | null;
  activeSessionMode: AcpActiveSessionMode;
  replayedMessages: AcpReplayedMessage[];
  sessionArtifacts: AcpSessionArtifactSummary[];
  selectedArtifact: AcpSessionArtifactContent | null;
  artifactsBusy: boolean;
  loadArtifactContent: (
    artifactName: string,
  ) => Promise<AcpSessionArtifactContent | null>;
  refreshArtifacts: () => Promise<void>;
  openArtifact: (artifactName: string) => Promise<void>;
  closeArtifact: () => void;
  sessionCaps: AcpSessionCapabilitiesFlags;
  sessionBusyId: string | null;
  error: string | null;
  authError: string | null;
  sessionError: string | null;
  clearAuthError: () => void;
  clearSessionError: () => void;
  authenticate: (
    methodId?: string,
    options?: AcpAuthenticateOptions,
  ) => Promise<void>;
  logout: () => Promise<void>;
  refreshSessions: () => Promise<void>;
  loadMoreSessions: () => Promise<void>;
  createNewSession: () => Promise<void>;
  loadExistingSession: (session: AcpSessionInfo) => Promise<void>;
  resumeExistingSession: (session: AcpSessionInfo) => Promise<void>;
  closeActiveSession: () => Promise<void>;
  deleteExistingSession: (sessionId: string) => Promise<void>;
  reconnect: () => void;
}

function appendMessageChunk(
  prev: AcpReplayedMessage[],
  id: string,
  role: "user" | "agent" | "thought",
  chunkText: string,
): AcpReplayedMessage[] {
  if (!chunkText) {
    return prev;
  }
  const existingIdx = prev.findIndex((item) => item.id === id);
  if (existingIdx >= 0) {
    return prev.map((item, idx) =>
      idx === existingIdx
        ? { ...item, text: sanitizeMultilineAgentText(item.text + chunkText) }
        : item,
    );
  }
  const last = prev[prev.length - 1];
  if (id.startsWith("auto_") && last && last.role === role) {
    return [
      ...prev.slice(0, -1),
      { ...last, text: sanitizeMultilineAgentText(last.text + chunkText) },
    ];
  }
  return [...prev, { id, role, text: chunkText }];
}

function mapRawToolStatus(rawStatus: unknown): AcpToolStatus {
  if (typeof rawStatus !== "string") {
    return "done";
  }
  const normalized = rawStatus.toLowerCase();
  if (normalized === "in_progress" || normalized === "running") {
    return "running";
  }
  if (normalized === "pending") {
    return "pending";
  }
  if (normalized === "failed" || normalized === "error") {
    return "error";
  }
  if (normalized === "denied" || normalized === "rejected") {
    return "denied";
  }
  return "done";
}

function extractToolText(update: Record<string, unknown>): string {
  const parts: string[] = [];
  if (Array.isArray(update.content)) {
    for (const entry of update.content) {
      if (typeof entry !== "object" || entry === null) {
        continue;
      }
      const record = entry as Record<string, unknown>;
      const inner = record.content as Record<string, unknown> | undefined;
      if (typeof record.text === "string") {
        parts.push(record.text);
      } else if (typeof inner?.text === "string") {
        parts.push(inner.text);
      } else if (typeof record.path === "string") {
        parts.push(record.path);
      }
    }
  }
  if (parts.length === 0 && typeof update.rawOutput === "string") {
    parts.push(update.rawOutput);
  } else if (
    parts.length === 0 &&
    typeof update.rawOutput === "object" &&
    update.rawOutput !== null
  ) {
    const rawOut = update.rawOutput as Record<string, unknown>;
    if (typeof rawOut.stdout === "string") {
      parts.push(rawOut.stdout);
    }
    if (typeof rawOut.stderr === "string" && rawOut.stderr) {
      parts.push(rawOut.stderr);
    }
  }
  return sanitizeMultilineAgentText(parts.join("\n"));
}

function extractToolLocations(update: Record<string, unknown>): string[] {
  if (!Array.isArray(update.locations)) {
    return [];
  }
  const locations: string[] = [];
  for (const loc of update.locations) {
    if (typeof loc === "string") {
      locations.push(sanitizeAgentText(loc, 260));
    } else if (typeof loc === "object" && loc !== null) {
      const pathVal = (loc as Record<string, unknown>).path;
      if (typeof pathVal === "string") {
        locations.push(sanitizeAgentText(pathVal, 260));
      }
    }
  }
  return locations.filter(Boolean);
}

function parsePlanEntries(rawEntries: unknown): AcpPlanStep[] {
  if (!Array.isArray(rawEntries)) {
    return [];
  }
  const steps: AcpPlanStep[] = [];
  for (const raw of rawEntries) {
    if (typeof raw !== "object" || raw === null) {
      continue;
    }
    const item = raw as Record<string, unknown>;
    const rawTitle =
      typeof item.content === "string"
        ? item.content
        : typeof item.title === "string"
          ? item.title
          : "";
    if (!rawTitle) {
      continue;
    }
    const rawStatus = typeof item.status === "string" ? item.status : "pending";
    const status: AcpPlanStep["status"] =
      rawStatus === "completed" || rawStatus === "done"
        ? "done"
        : rawStatus === "in_progress" || rawStatus === "running"
          ? "running"
          : "pending";
    steps.push({
      title: sanitizeAgentText(rawTitle, 200),
      status,
    });
  }
  return steps;
}

async function safeFetchArtifacts(
  sessionId: string,
): Promise<AcpSessionArtifactSummary[]> {
  try {
    const list = await fetchSessionArtifacts(sessionId);
    return list.artifacts;
  } catch {
    return [];
  }
}

export function useAcpConnection(): UseAcpConnectionState {
  const clientRef = useRef<AcpBrowserClient | null>(null);
  const defaultCwdRef = useRef<string>("");
  const chunkCounterRef = useRef(0);

  const [attempt, setAttempt] = useState(0);
  const [status, setStatus] = useState<AcpConnectionStatus>("initializing");
  const [result, setResult] = useState<AcpInitializeResult | null>(null);
  const [authStatus, setAuthStatus] = useState<AcpAuthStatus>("checking");
  const [activeMethodId, setActiveMethodId] = useState<string | null>(null);
  const [authenticatingMethodId, setAuthenticatingMethodId] = useState<
    string | null
  >(null);

  const [defaultCwd, setDefaultCwd] = useState("");
  const [agyAppDataDir, setAgyAppDataDir] = useState("antigravity");
  const [agyProfileDir, setAgyProfileDir] = useState("~/.gemini/antigravity");
  const [includeCbiMcp, setIncludeCbiMcp] = useState(true);
  const [sessions, setSessions] = useState<AcpSessionInfo[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [activeSessionMode, setActiveSessionMode] =
    useState<AcpActiveSessionMode>(null);
  const [replayedMessages, setReplayedMessages] = useState<
    AcpReplayedMessage[]
  >([]);
  const [sessionArtifacts, setSessionArtifacts] = useState<
    AcpSessionArtifactSummary[]
  >([]);
  const [selectedArtifact, setSelectedArtifact] =
    useState<AcpSessionArtifactContent | null>(null);
  const [artifactsBusy, setArtifactsBusy] = useState(false);
  const [sessionBusyId, setSessionBusyId] = useState<string | null>(null);

  const [error, setError] = useState<string | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  const [sessionError, setSessionError] = useState<string | null>(null);

  const clearAuthError = useCallback(() => setAuthError(null), []);
  const clearSessionError = useCallback(() => setSessionError(null), []);
  const closeArtifact = useCallback(() => setSelectedArtifact(null), []);

  const handleNotification = useCallback(
    (method: string, params: Record<string, unknown>) => {
      if (method !== "session/update") {
        return;
      }
      const sessionId =
        typeof params.sessionId === "string" ? params.sessionId : "";
      const update = params.update as Record<string, unknown> | undefined;
      if (!update) {
        return;
      }
      const updateKind =
        typeof update.sessionUpdate === "string" ? update.sessionUpdate : "";

      if (updateKind === "session_info_update" && sessionId) {
        const nextTitle =
          update.title === null
            ? null
            : typeof update.title === "string"
              ? sanitizeAgentText(update.title, 160)
              : undefined;
        const nextUpdated =
          update.updatedAt === null
            ? null
            : typeof update.updatedAt === "string"
              ? sanitizeAgentText(update.updatedAt, 64)
              : undefined;
        setSessions((prev) =>
          prev.map((item) =>
            item.sessionId === sessionId
              ? {
                  ...item,
                  ...(nextTitle !== undefined ? { title: nextTitle } : {}),
                  ...(nextUpdated !== undefined
                    ? { updatedAt: nextUpdated }
                    : {}),
                }
              : item,
          ),
        );
        return;
      }

      if (updateKind === "plan") {
        const planSteps = parsePlanEntries(update.entries);
        if (planSteps.length === 0) {
          return;
        }
        const planTitle =
          typeof update.title === "string" && update.title
            ? sanitizeAgentText(update.title, 140)
            : "Execution plan";
        setReplayedMessages((prev) => {
          const lastPlanIdx = [...prev]
            .reverse()
            .findIndex((item) => item.role === "plan");
          if (lastPlanIdx >= 0) {
            const targetIdx = prev.length - 1 - lastPlanIdx;
            return prev.map((item, idx) =>
              idx === targetIdx ? { ...item, planTitle, planSteps } : item,
            );
          }
          chunkCounterRef.current += 1;
          return [
            ...prev,
            {
              id: `plan_${chunkCounterRef.current}`,
              role: "plan",
              text: "",
              planTitle,
              planSteps,
            },
          ];
        });
        return;
      }

      if (updateKind === "tool_call" || updateKind === "tool_call_update") {
        const rawToolId =
          typeof update.toolCallId === "string" && update.toolCallId
            ? sanitizeAgentText(update.toolCallId, 96)
            : `tool_${chunkCounterRef.current++}`;
        const toolKind =
          typeof update.kind === "string"
            ? sanitizeAgentText(update.kind, 48)
            : undefined;
        const toolTitle =
          typeof update.title === "string" && update.title
            ? sanitizeAgentText(update.title, 160)
            : undefined;
        const toolStatus =
          update.status !== undefined
            ? mapRawToolStatus(update.status)
            : undefined;
        const text = extractToolText(update);
        const locations = extractToolLocations(update);
        const rawOut =
          typeof update.rawOutput === "object" && update.rawOutput !== null
            ? (update.rawOutput as Record<string, unknown>)
            : null;
        const exitCode =
          typeof rawOut?.exitCode === "number" ? rawOut.exitCode : undefined;

        setReplayedMessages((prev) => {
          const existingIdx = prev.findIndex((item) => item.id === rawToolId);
          if (existingIdx >= 0) {
            return prev.map((item, idx) =>
              idx === existingIdx
                ? {
                    ...item,
                    ...(toolKind ? { toolKind } : {}),
                    ...(toolTitle ? { toolTitle } : {}),
                    ...(toolStatus ? { toolStatus } : {}),
                    ...(text ? { text } : {}),
                    ...(locations.length > 0 ? { locations } : {}),
                    ...(exitCode !== undefined ? { exitCode } : {}),
                  }
                : item,
            );
          }
          return [
            ...prev,
            {
              id: rawToolId,
              role: "tool",
              text,
              toolKind,
              toolTitle,
              toolStatus: toolStatus ?? "done",
              locations: locations.length > 0 ? locations : undefined,
              exitCode,
            },
          ];
        });
        return;
      }

      const roleMap: Record<string, "user" | "agent" | "thought" | undefined> =
        {
          user_message_chunk: "user",
          agent_message_chunk: "agent",
          agent_thought_chunk: "thought",
        };
      const role = roleMap[updateKind];
      if (!role) {
        return;
      }
      const content = update.content as Record<string, unknown> | undefined;
      if (content?.type !== "text" || typeof content.text !== "string") {
        return;
      }
      const cleanChunk = sanitizeMultilineAgentText(content.text);
      const rawMsgId =
        typeof update.messageId === "string" && update.messageId
          ? sanitizeAgentText(update.messageId, 96)
          : `auto_${chunkCounterRef.current}`;
      if (rawMsgId.startsWith("auto_")) {
        chunkCounterRef.current += 1;
      }
      setReplayedMessages((prev) =>
        appendMessageChunk(prev, rawMsgId, role, cleanChunk),
      );
    },
    [],
  );

  const reconnect = useCallback(() => {
    setStatus("initializing");
    setAuthStatus("checking");
    setError(null);
    setAuthError(null);
    setSessionError(null);
    setActiveMethodId(null);
    setAuthenticatingMethodId(null);
    setSessions([]);
    setNextCursor(null);
    setActiveSessionId(null);
    setActiveSessionMode(null);
    setReplayedMessages([]);
    setSessionArtifacts([]);
    setSelectedArtifact(null);
    setAttempt((current) => current + 1);
  }, []);

  useEffect(() => {
    let disposed = false;
    const client = new AcpBrowserClient({ onNotification: handleNotification });
    clientRef.current = client;

    Promise.all([client.connectAndInitialize(), fetchServerDefaults()])
      .then(async ([initResult, serverDefaults]) => {
        if (disposed) {
          return;
        }
        defaultCwdRef.current = serverDefaults.defaultCwd;
        setDefaultCwd(serverDefaults.defaultCwd);
        setAgyAppDataDir(serverDefaults.agyAppDataDir);
        setAgyProfileDir(serverDefaults.agyProfileDir);
        setIncludeCbiMcp(serverDefaults.cbiMcpEnabled);
        setResult(initResult);
        setStatus("ready");

        if (!deriveSessionCapabilities(initResult).canList) {
          setAuthStatus("unauthenticated");
          return;
        }
        try {
          const page = await client.listSessions(
            serverDefaults.defaultCwd
              ? { cwd: serverDefaults.defaultCwd }
              : undefined,
          );
          if (!disposed) {
            setSessions(page.sessions);
            setNextCursor(page.nextCursor ?? null);
            setAuthStatus("authenticated");
          }
        } catch (err: unknown) {
          if (disposed) {
            return;
          }
          if (err instanceof AcpRpcError && err.code === ACP_AUTH_REQUIRED_CODE) {
            setSessions([]);
            setNextCursor(null);
            setAuthStatus("unauthenticated");
            return;
          }
          throw err;
        }
      })
      .catch((err: unknown) => {
        if (disposed) {
          return;
        }
        const message =
          err instanceof Error ? err.message : "Failed to initialize ACP.";
        setResult(null);
        setError(message);
        setStatus("error");
      });

    return () => {
      disposed = true;
      if (clientRef.current === client) {
        clientRef.current = null;
      }
      client.close();
    };
  }, [attempt, handleNotification]);

  const refreshSessions = useCallback(async () => {
    const client = clientRef.current;
    if (!client) {
      return;
    }
    setSessionError(null);
    setSessionBusyId("list");
    try {
      const cwd = defaultCwdRef.current;
      const page = await client.listSessions(cwd ? { cwd } : undefined);
      setSessions(page.sessions);
      setNextCursor(page.nextCursor ?? null);
    } catch (err: unknown) {
      if (err instanceof AcpRpcError && err.code === ACP_AUTH_REQUIRED_CODE) {
        setAuthStatus("unauthenticated");
        setSessions([]);
        return;
      }
      setSessionError(
        err instanceof Error ? err.message : "Failed to list sessions.",
      );
    } finally {
      setSessionBusyId(null);
    }
  }, []);

  const loadMoreSessions = useCallback(async () => {
    const client = clientRef.current;
    if (!client || !nextCursor) {
      return;
    }
    setSessionError(null);
    setSessionBusyId("more");
    try {
      const cwd = defaultCwdRef.current;
      const page = await client.listSessions({
        ...(cwd ? { cwd } : {}),
        cursor: nextCursor,
      });
      setSessions((prev) => [...prev, ...page.sessions]);
      setNextCursor(page.nextCursor ?? null);
    } catch (err: unknown) {
      setSessionError(
        err instanceof Error ? err.message : "Failed to load more sessions.",
      );
    } finally {
      setSessionBusyId(null);
    }
  }, [nextCursor]);

  const authenticate = useCallback(
    async (
      methodId: string = DEFAULT_AUTH_METHOD_ID,
      options?: AcpAuthenticateOptions,
    ) => {
      const client = clientRef.current;
      if (!client) {
        return;
      }
      setAuthError(null);
      setAuthenticatingMethodId(methodId);
      setAuthStatus("authenticating");
      try {
        await client.authenticate(methodId, options);
        setActiveMethodId(methodId);
        setAuthenticatingMethodId(null);
        setAuthStatus("authenticated");
        await refreshSessions();
      } catch (err: unknown) {
        const message =
          err instanceof Error ? err.message : "ACP authentication failed.";
        setAuthenticatingMethodId(null);
        setAuthStatus("unauthenticated");
        setAuthError(message);
      }
    },
    [refreshSessions],
  );

  const logout = useCallback(async () => {
    const client = clientRef.current;
    if (!client) {
      return;
    }
    setAuthError(null);
    setAuthStatus("logging_out");
    try {
      await client.logout();
      setActiveMethodId(null);
      setSessions([]);
      setNextCursor(null);
      setActiveSessionId(null);
      setActiveSessionMode(null);
      setReplayedMessages([]);
      setSessionArtifacts([]);
      setSelectedArtifact(null);
      setAuthStatus("unauthenticated");
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : "Failed to log out from ACP.";
      setAuthStatus("authenticated");
      setAuthError(message);
    }
  }, []);

  const createNewSession = useCallback(async () => {
    const client = clientRef.current;
    const cwd = defaultCwdRef.current;
    if (!client || !cwd) {
      setSessionError("Workspace directory (cwd) is unavailable.");
      return;
    }
    setSessionError(null);
    setSessionBusyId("new");
    try {
      const created = await client.createSession(cwd, { includeCbiMcp });
      setReplayedMessages([]);
      setSessionArtifacts([]);
      setSelectedArtifact(null);
      setActiveSessionId(created.sessionId);
      setActiveSessionMode("new");
      setSessions((prev) => [
        {
          sessionId: created.sessionId,
          cwd,
          title: "New session",
          updatedAt: new Date().toISOString(),
        },
        ...prev.filter((item) => item.sessionId !== created.sessionId),
      ]);
    } catch (err: unknown) {
      if (err instanceof AcpRpcError && err.code === ACP_AUTH_REQUIRED_CODE) {
        setAuthStatus("unauthenticated");
        return;
      }
      setSessionError(
        err instanceof Error ? err.message : "Failed to create session.",
      );
    } finally {
      setSessionBusyId(null);
    }
  }, [includeCbiMcp]);

  const loadExistingSession = useCallback(
    async (session: AcpSessionInfo) => {
      const client = clientRef.current;
      if (!client) {
        return;
      }
      setSessionError(null);
      setSessionBusyId(session.sessionId);
      setReplayedMessages([]);
      setSessionArtifacts([]);
      setSelectedArtifact(null);
      try {
        const [, artifacts] = await Promise.all([
          client.loadSession(session.sessionId, session.cwd, {
            includeCbiMcp,
          }),
          safeFetchArtifacts(session.sessionId),
        ]);
        setActiveSessionId(session.sessionId);
        setActiveSessionMode("loaded");
        setSessionArtifacts(artifacts);
      } catch (err: unknown) {
        setSessionError(
          err instanceof Error ? err.message : "Failed to load session.",
        );
      } finally {
        setSessionBusyId(null);
      }
    },
    [includeCbiMcp],
  );

  const resumeExistingSession = useCallback(
    async (session: AcpSessionInfo) => {
      const client = clientRef.current;
      if (!client) {
        return;
      }
      setSessionError(null);
      setSessionBusyId(session.sessionId);
      setReplayedMessages([]);
      setSessionArtifacts([]);
      setSelectedArtifact(null);
      try {
        const [, artifacts] = await Promise.all([
          client.resumeSession(session.sessionId, session.cwd, {
            includeCbiMcp,
          }),
          safeFetchArtifacts(session.sessionId),
        ]);
        setActiveSessionId(session.sessionId);
        setActiveSessionMode("resumed");
        setSessionArtifacts(artifacts);
      } catch (err: unknown) {
        setSessionError(
          err instanceof Error ? err.message : "Failed to resume session.",
        );
      } finally {
        setSessionBusyId(null);
      }
    },
    [includeCbiMcp],
  );

  const closeActiveSession = useCallback(async () => {
    const client = clientRef.current;
    if (!client || !activeSessionId) {
      return;
    }
    setSessionError(null);
    setSessionBusyId("close");
    try {
      await client.closeSession(activeSessionId);
      setActiveSessionId(null);
      setActiveSessionMode(null);
      setReplayedMessages([]);
      setSessionArtifacts([]);
      setSelectedArtifact(null);
    } catch (err: unknown) {
      setSessionError(
        err instanceof Error ? err.message : "Failed to close session.",
      );
    } finally {
      setSessionBusyId(null);
    }
  }, [activeSessionId]);

  const deleteExistingSession = useCallback(
    async (sessionId: string) => {
      const client = clientRef.current;
      if (!client) {
        return;
      }
      setSessionError(null);
      setSessionBusyId(sessionId);
      try {
        await client.deleteSession(sessionId);
        setSessions((prev) =>
          prev.filter((item) => item.sessionId !== sessionId),
        );
        if (activeSessionId === sessionId) {
          setActiveSessionId(null);
          setActiveSessionMode(null);
          setReplayedMessages([]);
          setSessionArtifacts([]);
          setSelectedArtifact(null);
        }
      } catch (err: unknown) {
        setSessionError(
          err instanceof Error ? err.message : "Failed to delete session.",
        );
      } finally {
        setSessionBusyId(null);
      }
    },
    [activeSessionId],
  );

  const refreshArtifacts = useCallback(async () => {
    if (!activeSessionId) {
      return;
    }
    setArtifactsBusy(true);
    try {
      const list = await fetchSessionArtifacts(activeSessionId);
      setSessionArtifacts(list.artifacts);
    } catch (err: unknown) {
      setSessionError(
        err instanceof Error ? err.message : "Failed to list artifacts.",
      );
    } finally {
      setArtifactsBusy(false);
    }
  }, [activeSessionId]);

  const loadArtifactContent = useCallback(
    async (artifactName: string): Promise<AcpSessionArtifactContent | null> => {
      if (!activeSessionId) {
        return null;
      }
      try {
        return await fetchSessionArtifactContent(activeSessionId, artifactName);
      } catch {
        return null;
      }
    },
    [activeSessionId],
  );

  const openArtifact = useCallback(
    async (artifactName: string) => {
      if (!activeSessionId) {
        return;
      }
      setArtifactsBusy(true);
      try {
        const content = await fetchSessionArtifactContent(
          activeSessionId,
          artifactName,
        );
        setSelectedArtifact(content);
      } catch (err: unknown) {
        setSessionError(
          err instanceof Error ? err.message : "Failed to read artifact.",
        );
      } finally {
        setArtifactsBusy(false);
      }
    },
    [activeSessionId],
  );

  const sessionCaps = useMemo(
    () => deriveSessionCapabilities(result),
    [result],
  );
  const canLogout = Boolean(result?.agentCapabilities?.auth?.logout);

  return {
    status,
    result,
    authStatus,
    activeMethodId,
    authenticatingMethodId,
    canLogout,
    defaultCwd,
    agyAppDataDir,
    agyProfileDir,
    includeCbiMcp,
    setIncludeCbiMcp,
    sessions,
    nextCursor,
    activeSessionId,
    activeSessionMode,
    replayedMessages,
    sessionArtifacts,
    selectedArtifact,
    artifactsBusy,
    loadArtifactContent,
    refreshArtifacts,
    openArtifact,
    closeArtifact,
    sessionCaps,
    sessionBusyId,
    error,
    authError,
    sessionError,
    clearAuthError,
    clearSessionError,
    authenticate,
    logout,
    refreshSessions,
    loadMoreSessions,
    createNewSession,
    loadExistingSession,
    resumeExistingSession,
    closeActiveSession,
    deleteExistingSession,
    reconnect,
  };
}
