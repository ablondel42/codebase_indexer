import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  AcpBrowserClient,
  AcpRpcError,
  deriveSessionCapabilities,
  fetchServerDefaults,
} from "@/lib/acp/client";
import {
  sanitizeAgentText,
  sanitizeMultilineAgentText,
} from "@/lib/acp/security";
import {
  ACP_AUTH_REQUIRED_CODE,
  DEFAULT_AUTH_METHOD_ID,
  type AcpAuthenticateOptions,
  type AcpAuthStatus,
  type AcpConnectionStatus,
  type AcpInitializeResult,
  type AcpReplayedMessage,
  type AcpSessionCapabilitiesFlags,
  type AcpSessionInfo,
} from "@/lib/acp/types";

export interface UseAcpConnectionState {
  status: AcpConnectionStatus;
  result: AcpInitializeResult | null;
  authStatus: AcpAuthStatus;
  activeMethodId: string | null;
  authenticatingMethodId: string | null;
  canLogout: boolean;
  defaultCwd: string;
  includeCbiMcp: boolean;
  setIncludeCbiMcp: (enabled: boolean) => void;
  sessions: AcpSessionInfo[];
  nextCursor: string | null;
  activeSessionId: string | null;
  replayedMessages: AcpReplayedMessage[];
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
  role: AcpReplayedMessage["role"],
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
  const [includeCbiMcp, setIncludeCbiMcp] = useState(true);
  const [sessions, setSessions] = useState<AcpSessionInfo[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [replayedMessages, setReplayedMessages] = useState<
    AcpReplayedMessage[]
  >([]);
  const [sessionBusyId, setSessionBusyId] = useState<string | null>(null);

  const [error, setError] = useState<string | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  const [sessionError, setSessionError] = useState<string | null>(null);

  const clearAuthError = useCallback(() => setAuthError(null), []);
  const clearSessionError = useCallback(() => setSessionError(null), []);

  const handleNotification = useCallback(
    (method: string, params: Record<string, unknown>) => {
      if (method !== "session/update") {
        return;
      }
      const sessionId =
        typeof params.sessionId === "string" ? params.sessionId : "";
      const update = params.update as Record<string, unknown> | undefined;
      const updateKind =
        typeof update?.sessionUpdate === "string" ? update.sessionUpdate : "";

      if (updateKind === "session_info_update" && sessionId) {
        const nextTitle =
          update?.title === null
            ? null
            : typeof update?.title === "string"
              ? sanitizeAgentText(update.title, 160)
              : undefined;
        const nextUpdated =
          update?.updatedAt === null
            ? null
            : typeof update?.updatedAt === "string"
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

      const roleMap: Record<string, AcpReplayedMessage["role"] | undefined> = {
        user_message_chunk: "user",
        agent_message_chunk: "agent",
        agent_thought_chunk: "thought",
      };
      const role = roleMap[updateKind];
      if (!role) {
        return;
      }
      const content = update?.content as Record<string, unknown> | undefined;
      if (content?.type !== "text" || typeof content.text !== "string") {
        return;
      }
      const cleanChunk = sanitizeMultilineAgentText(content.text);
      const rawMsgId =
        typeof update?.messageId === "string" && update.messageId
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
    setReplayedMessages([]);
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
      setReplayedMessages([]);
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
      setActiveSessionId(created.sessionId);
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
      try {
        await client.loadSession(session.sessionId, session.cwd, {
          includeCbiMcp,
        });
        setActiveSessionId(session.sessionId);
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
      try {
        await client.resumeSession(session.sessionId, session.cwd, {
          includeCbiMcp,
        });
        setActiveSessionId(session.sessionId);
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
      setReplayedMessages([]);
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
          setReplayedMessages([]);
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
    includeCbiMcp,
    setIncludeCbiMcp,
    sessions,
    nextCursor,
    activeSessionId,
    replayedMessages,
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
