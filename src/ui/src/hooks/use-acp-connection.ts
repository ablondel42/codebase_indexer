import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  AcpBrowserClient,
  AcpRpcError,
  deriveSessionCapabilities,
  fetchServerDefaults,
  fetchSessionArtifactContent,
  fetchSessionArtifacts,
  fetchSessionMessages,
  sanitizeAvailableCommands,
  sanitizeConfigOptions,
  sanitizeSessionUsage,
} from "@/lib/acp/client";
import {
  sanitizeAgentText,
  sanitizeMultilineAgentText,
} from "@/lib/acp/security";
import {
  ACP_AUTH_REQUIRED_CODE,
  DEFAULT_AUTH_METHOD_ID,
  type AcpActiveSessionMode,
  type AcpAntigravityTier,
  type AcpAuthenticateOptions,
  type AcpAuthStatus,
  type AcpAvailableCommand,
  type AcpConfigOption,
  type AcpConnectionStatus,
  type AcpInitializeResult,
  type AcpModelsState,
  type AcpModesState,
  type AcpPlanStep,
  type AcpReplayedMessage,
  type AcpSessionArtifactContent,
  type AcpSessionArtifactSummary,
  type AcpSessionCapabilitiesFlags,
  type AcpSessionInfo,
  type AcpSessionMessagesPage,
  type AcpSessionRuntimeMeta,
  type AcpSessionSetupResult,
  type AcpSessionUsage,
  type AcpToolStatus,
} from "@/lib/acp/types";

const EMPTY_SESSION_META: AcpSessionRuntimeMeta = {
  models: null,
  modes: null,
  configOptions: [],
  availableCommands: [],
  usage: null,
  tier: null,
};

function syncModesWithOptions(
  modes: AcpModesState | null,
  options: AcpConfigOption[],
): AcpModesState | null {
  if (!modes) {
    return null;
  }
  const modeOpt = options.find(
    (opt) =>
      (opt.category === "mode" || opt.id === "mode") &&
      typeof opt.currentValue === "string",
  );
  if (!modeOpt || typeof modeOpt.currentValue !== "string") {
    return modes;
  }
  return { ...modes, currentModeId: modeOpt.currentValue };
}

export interface UseAcpConnectionState {
  status: AcpConnectionStatus;
  result: AcpInitializeResult | null;
  authStatus: AcpAuthStatus;
  activeMethodId: string | null;
  authenticatingMethodId: string | null;
  canLogout: boolean;
  accountTier: AcpAntigravityTier | null;
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
  hasMoreMessages: boolean;
  loadingMessages: boolean;
  loadingOlderMessages: boolean;
  isLiveThinking: boolean;
  totalMessageItems: number;
  loadOlderMessages: () => Promise<void>;
  sessionModels: AcpModelsState | null;
  sessionModes: AcpModesState | null;
  configOptions: AcpConfigOption[];
  availableCommands: AcpAvailableCommand[];
  sessionUsage: AcpSessionUsage | null;
  configBusyId: string | null;
  sessionArtifacts: AcpSessionArtifactSummary[];
  selectedArtifact: AcpSessionArtifactContent | null;
  artifactsBusy: boolean;
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
  resumeExistingSession: (session: AcpSessionInfo) => Promise<void>;
  setSessionConfigOption: (
    configId: string,
    value: string | boolean,
  ) => Promise<void>;
  setSessionMode: (modeId: string) => Promise<void>;
  closeActiveSession: () => Promise<void>;
  deleteExistingSession: (sessionId: string) => Promise<void>;
  reconnect: () => void;
}

type LiveMessageOp =
  | {
      kind: "chunk";
      sessionId: string;
      id: string;
      role: "user" | "agent" | "thought";
      text: string;
    }
  | {
      kind: "tool";
      sessionId: string;
      id: string;
      toolKind?: string;
      toolTitle?: string;
      toolStatus?: AcpToolStatus;
      text: string;
      locations: string[];
      exitCode?: number;
    }
  | {
      kind: "plan";
      sessionId: string;
      planTitle: string;
      planSteps: AcpPlanStep[];
    };

interface KnownTimelineTracker {
  ids: Set<string>;
  lastRole: AcpReplayedMessage["role"] | null;
  lastId: string | null;
}

function syncKnownTracker(
  tracker: KnownTimelineTracker,
  items: AcpReplayedMessage[],
): void {
  tracker.ids = new Set(items.map((item) => item.id));
  const tail = items[items.length - 1];
  tracker.lastRole = tail?.role ?? null;
  tracker.lastId = tail?.id ?? null;
}

function countAddedLiveOps(
  ops: LiveMessageOp[],
  tracker: KnownTimelineTracker,
): number {
  let added = 0;
  for (const op of ops) {
    if (op.kind === "chunk") {
      if (!op.text) {
        continue;
      }
      const matchesTail =
        tracker.lastId === op.id ||
        (op.id.startsWith("auto_") && tracker.lastRole === op.role);
      if (!matchesTail && !tracker.ids.has(op.id)) {
        added += 1;
        tracker.ids.add(op.id);
        tracker.lastId = op.id;
      }
      tracker.lastRole = op.role;
      continue;
    }
    if (op.kind === "tool") {
      if (!tracker.ids.has(op.id)) {
        added += 1;
        tracker.ids.add(op.id);
        tracker.lastId = op.id;
        tracker.lastRole = "tool";
      }
      continue;
    }
    if (tracker.lastRole !== "plan") {
      added += 1;
      tracker.lastRole = "plan";
    }
  }
  return added;
}

function applyLiveOps(
  prev: AcpReplayedMessage[],
  ops: LiveMessageOp[],
  nextPlanId: () => string,
): AcpReplayedMessage[] {
  let next = prev;
  let copied = false;
  const ensureCopy = () => {
    if (!copied) {
      next = [...prev];
      copied = true;
    }
    return next;
  };

  for (const op of ops) {
    if (op.kind === "chunk") {
      if (!op.text) {
        continue;
      }
      const tail = next[next.length - 1];
      if (
        tail &&
        (tail.id === op.id ||
          (op.id.startsWith("auto_") && tail.role === op.role))
      ) {
        const arr = ensureCopy();
        arr[arr.length - 1] = { ...tail, text: tail.text + op.text };
        continue;
      }
      const idx = op.id.startsWith("auto_")
        ? -1
        : next.findIndex((item) => item.id === op.id);
      if (idx >= 0) {
        const arr = ensureCopy();
        const existing = arr[idx]!;
        arr[idx] = { ...existing, text: existing.text + op.text };
      } else {
        const arr = ensureCopy();
        arr.push({ id: op.id, role: op.role, text: op.text });
      }
      continue;
    }

    if (op.kind === "tool") {
      const tail = next[next.length - 1];
      const idx =
        tail?.id === op.id
          ? next.length - 1
          : next.findIndex((item) => item.id === op.id);
      if (idx >= 0) {
        const arr = ensureCopy();
        const existing = arr[idx]!;
        arr[idx] = {
          ...existing,
          ...(op.toolKind ? { toolKind: op.toolKind } : {}),
          ...(op.toolTitle ? { toolTitle: op.toolTitle } : {}),
          ...(op.toolStatus ? { toolStatus: op.toolStatus } : {}),
          ...(op.text ? { text: op.text } : {}),
          ...(op.locations.length > 0 ? { locations: op.locations } : {}),
          ...(op.exitCode !== undefined ? { exitCode: op.exitCode } : {}),
        };
      } else {
        const arr = ensureCopy();
        arr.push({
          id: op.id,
          role: "tool",
          text: op.text,
          toolKind: op.toolKind,
          toolTitle: op.toolTitle,
          toolStatus: op.toolStatus ?? "done",
          locations: op.locations.length > 0 ? op.locations : undefined,
          exitCode: op.exitCode,
        });
      }
      continue;
    }

    let lastPlanIdx = -1;
    for (let i = next.length - 1; i >= 0; i -= 1) {
      if (next[i]?.role === "plan") {
        lastPlanIdx = i;
        break;
      }
    }
    if (lastPlanIdx >= 0) {
      const arr = ensureCopy();
      arr[lastPlanIdx] = {
        ...arr[lastPlanIdx]!,
        planTitle: op.planTitle,
        planSteps: op.planSteps,
      };
    } else {
      const arr = ensureCopy();
      arr.push({
        id: nextPlanId(),
        role: "plan",
        text: "",
        planTitle: op.planTitle,
        planSteps: op.planSteps,
      });
    }
  }

  return next;
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
  const activeSessionIdRef = useRef<string | null>(null);
  const nextBeforeTurnRef = useRef<number | null>(null);
  const loadingOlderRef = useRef(false);
  const pendingLiveOpsRef = useRef<LiveMessageOp[]>([]);
  const rafFlushIdRef = useRef<number | null>(null);
  const knownTrackerRef = useRef<KnownTimelineTracker>({
    ids: new Set(),
    lastRole: null,
    lastId: null,
  });

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
  const [hasMoreMessages, setHasMoreMessages] = useState(false);
  const [loadingMessages, setLoadingMessages] = useState(false);
  const [loadingOlderMessages, setLoadingOlderMessages] = useState(false);
  const [isLiveThinking, setIsLiveThinking] = useState(false);
  const [totalMessageItems, setTotalMessageItems] = useState(0);
  const [sessionMetaById, setSessionMetaById] = useState<
    Record<string, AcpSessionRuntimeMeta>
  >({});
  const [accountTier, setAccountTier] = useState<AcpAntigravityTier | null>(
    null,
  );
  const [configBusyId, setConfigBusyId] = useState<string | null>(null);
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

  const cancelPendingLiveFlush = useCallback(() => {
    if (rafFlushIdRef.current !== null) {
      window.cancelAnimationFrame(rafFlushIdRef.current);
      rafFlushIdRef.current = null;
    }
    pendingLiveOpsRef.current = [];
  }, []);

  const resetTimelineState = useCallback(
    (nextSessionId: string | null) => {
      cancelPendingLiveFlush();
      activeSessionIdRef.current = nextSessionId;
      nextBeforeTurnRef.current = null;
      loadingOlderRef.current = false;
      knownTrackerRef.current = { ids: new Set(), lastRole: null, lastId: null };
      setActiveSessionId(nextSessionId);
      setReplayedMessages([]);
      setHasMoreMessages(false);
      setLoadingMessages(false);
      setLoadingOlderMessages(false);
      setIsLiveThinking(false);
      setTotalMessageItems(0);
    },
    [cancelPendingLiveFlush],
  );

  const enqueueLiveOp = useCallback((op: LiveMessageOp) => {
    pendingLiveOpsRef.current.push(op);
    if (rafFlushIdRef.current !== null) {
      return;
    }
    rafFlushIdRef.current = window.requestAnimationFrame(() => {
      rafFlushIdRef.current = null;
      const queued = pendingLiveOpsRef.current;
      pendingLiveOpsRef.current = [];
      const currentSid = activeSessionIdRef.current;
      const relevant = currentSid
        ? queued.filter((item) => item.sessionId === currentSid)
        : queued;
      if (relevant.length === 0) {
        return;
      }
      const added = countAddedLiveOps(relevant, knownTrackerRef.current);
      const lastOp = relevant[relevant.length - 1];
      if (lastOp) {
        setIsLiveThinking(lastOp.kind === "chunk" && lastOp.role === "thought");
      }
      setReplayedMessages((prev) =>
        applyLiveOps(prev, relevant, () => {
          chunkCounterRef.current += 1;
          return `plan_${chunkCounterRef.current}`;
        }),
      );
      if (added > 0) {
        setTotalMessageItems((prev) => prev + added);
      }
    });
  }, []);

  const mergeSetupMetadata = useCallback((setup: AcpSessionSetupResult) => {
    if (setup.tier) {
      setAccountTier(setup.tier);
    }
    setSessionMetaById((prev) => {
      const base = prev[setup.sessionId] ?? EMPTY_SESSION_META;
      const configOptions =
        setup.configOptions.length > 0
          ? setup.configOptions
          : base.configOptions;
      const modes = syncModesWithOptions(
        setup.modes ?? base.modes,
        configOptions,
      );
      return {
        ...prev,
        [setup.sessionId]: {
          models: setup.models ?? base.models,
          modes,
          configOptions,
          availableCommands: base.availableCommands,
          usage: base.usage,
          tier: setup.tier ?? base.tier,
        },
      };
    });
  }, []);

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

      if (updateKind === "available_commands_update" && sessionId) {
        const commands = sanitizeAvailableCommands(update.availableCommands);
        setSessionMetaById((prev) => {
          const base = prev[sessionId] ?? EMPTY_SESSION_META;
          return {
            ...prev,
            [sessionId]: { ...base, availableCommands: commands },
          };
        });
        return;
      }

      if (updateKind === "usage_update" && sessionId) {
        const usage = sanitizeSessionUsage(update);
        if (usage) {
          setSessionMetaById((prev) => {
            const base = prev[sessionId] ?? EMPTY_SESSION_META;
            return { ...prev, [sessionId]: { ...base, usage } };
          });
        }
        return;
      }

      if (updateKind === "config_option_update" && sessionId) {
        const nextOptions = sanitizeConfigOptions(update.configOptions);
        setSessionMetaById((prev) => {
          const base = prev[sessionId] ?? EMPTY_SESSION_META;
          return {
            ...prev,
            [sessionId]: {
              ...base,
              configOptions: nextOptions,
              modes: syncModesWithOptions(base.modes, nextOptions),
            },
          };
        });
        return;
      }

      if (updateKind === "current_mode_update" && sessionId) {
        const modeId =
          typeof update.currentModeId === "string"
            ? sanitizeAgentText(update.currentModeId, 96)
            : "";
        if (modeId) {
          setSessionMetaById((prev) => {
            const base = prev[sessionId] ?? EMPTY_SESSION_META;
            const nextModes: AcpModesState = base.modes
              ? { ...base.modes, currentModeId: modeId }
              : { currentModeId: modeId, availableModes: [] };
            const nextOptions = base.configOptions.map((opt) =>
              opt.category === "mode" || opt.id === "mode"
                ? { ...opt, currentValue: modeId }
                : opt,
            );
            return {
              ...prev,
              [sessionId]: {
                ...base,
                modes: nextModes,
                configOptions: nextOptions,
              },
            };
          });
        }
        return;
      }

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

      const targetSid = sessionId || activeSessionIdRef.current || "";
      if (
        !targetSid ||
        (activeSessionIdRef.current && targetSid !== activeSessionIdRef.current)
      ) {
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
        enqueueLiveOp({
          kind: "plan",
          sessionId: targetSid,
          planTitle,
          planSteps,
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

        enqueueLiveOp({
          kind: "tool",
          sessionId: targetSid,
          id: rawToolId,
          toolKind,
          toolTitle,
          toolStatus,
          text,
          locations,
          exitCode,
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
      enqueueLiveOp({
        kind: "chunk",
        sessionId: targetSid,
        id: rawMsgId,
        role,
        text: cleanChunk,
      });
    },
    [enqueueLiveOp],
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
    setActiveSessionMode(null);
    resetTimelineState(null);
    setSessionMetaById({});
    setAccountTier(null);
    setConfigBusyId(null);
    setSessionArtifacts([]);
    setSelectedArtifact(null);
    setAttempt((current) => current + 1);
  }, [resetTimelineState]);

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
      cancelPendingLiveFlush();
      if (clientRef.current === client) {
        clientRef.current = null;
      }
      client.close();
    };
  }, [attempt, cancelPendingLiveFlush, handleNotification]);

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
      setActiveSessionMode(null);
      resetTimelineState(null);
      setSessionMetaById({});
      setAccountTier(null);
      setConfigBusyId(null);
      setSessionArtifacts([]);
      setSelectedArtifact(null);
      setAuthStatus("unauthenticated");
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : "Failed to log out from ACP.";
      setAuthStatus("authenticated");
      setAuthError(message);
    }
  }, [resetTimelineState]);

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
      mergeSetupMetadata(created);
      setSessionArtifacts([]);
      setSelectedArtifact(null);
      resetTimelineState(created.sessionId);
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
  }, [includeCbiMcp, mergeSetupMetadata, resetTimelineState]);

  const applyInitialMessagesPage = useCallback(
    (page: AcpSessionMessagesPage) => {
      nextBeforeTurnRef.current = page.nextBeforeTurn;
      setHasMoreMessages(page.hasMore);
      setTotalMessageItems(page.totalItems);
      setReplayedMessages((prev) => {
        if (prev.length === 0) {
          syncKnownTracker(knownTrackerRef.current, page.items);
          return page.items;
        }
        const pageIds = new Set(page.items.map((item) => item.id));
        const trailingLive = prev.filter((item) => !pageIds.has(item.id));
        const merged = [...page.items, ...trailingLive];
        syncKnownTracker(knownTrackerRef.current, merged);
        return merged;
      });
    },
    [],
  );

  const resumeExistingSession = useCallback(
    async (session: AcpSessionInfo) => {
      const client = clientRef.current;
      if (!client) {
        return;
      }
      const targetId = session.sessionId;
      setSessionError(null);
      setSessionBusyId(targetId);
      resetTimelineState(targetId);
      setLoadingMessages(true);
      setSessionArtifacts([]);
      setSelectedArtifact(null);

      const initialTailPromise = fetchSessionMessages(targetId, {
        limitTurns: 8,
      })
        .then((page) => {
          if (activeSessionIdRef.current === targetId && page.hydrated) {
            applyInitialMessagesPage(page);
            setLoadingMessages(false);
          }
          return page;
        })
        .catch(() => null);

      try {
        const [resumed, artifacts, initialPage] = await Promise.all([
          client.resumeSession(targetId, session.cwd, {
            includeCbiMcp,
          }),
          safeFetchArtifacts(targetId),
          initialTailPromise,
        ]);
        if (activeSessionIdRef.current !== targetId) {
          return;
        }
        mergeSetupMetadata(resumed);
        setActiveSessionMode(resumed.replayed ? "loaded" : "resumed");
        setSessionArtifacts(artifacts);

        if (!initialPage || !initialPage.hydrated) {
          const postHydratePage = await fetchSessionMessages(targetId, {
            limitTurns: 8,
          });
          if (activeSessionIdRef.current === targetId) {
            applyInitialMessagesPage(postHydratePage);
          }
        }
      } catch (err: unknown) {
        if (activeSessionIdRef.current === targetId) {
          setSessionError(
            err instanceof Error ? err.message : "Failed to resume session.",
          );
        }
      } finally {
        if (activeSessionIdRef.current === targetId) {
          setLoadingMessages(false);
        }
        setSessionBusyId(null);
      }
    },
    [applyInitialMessagesPage, includeCbiMcp, mergeSetupMetadata, resetTimelineState],
  );

  const loadOlderMessages = useCallback(async () => {
    const targetId = activeSessionIdRef.current;
    const beforeTurn = nextBeforeTurnRef.current;
    if (!targetId || beforeTurn === null || loadingOlderRef.current) {
      return;
    }
    loadingOlderRef.current = true;
    setLoadingOlderMessages(true);
    try {
      const page = await fetchSessionMessages(targetId, {
        limitTurns: 10,
        beforeTurn,
      });
      if (activeSessionIdRef.current !== targetId) {
        return;
      }
      nextBeforeTurnRef.current = page.nextBeforeTurn;
      setHasMoreMessages(page.hasMore);
      setTotalMessageItems((prev) => Math.max(prev, page.totalItems));
      if (page.items.length > 0) {
        setReplayedMessages((prev) => {
          const existingIds = new Set(prev.map((item) => item.id));
          const olderUnique = page.items.filter(
            (item) => !existingIds.has(item.id),
          );
          if (olderUnique.length === 0) {
            return prev;
          }
          const combined = [...olderUnique, ...prev];
          syncKnownTracker(knownTrackerRef.current, combined);
          return combined;
        });
      }
    } catch (err: unknown) {
      if (activeSessionIdRef.current === targetId) {
        setSessionError(
          err instanceof Error
            ? err.message
            : "Failed to load earlier messages.",
        );
      }
    } finally {
      if (activeSessionIdRef.current === targetId) {
        loadingOlderRef.current = false;
        setLoadingOlderMessages(false);
      }
    }
  }, []);

  const setSessionConfigOption = useCallback(
    async (configId: string, value: string | boolean) => {
      const client = clientRef.current;
      if (!client || !activeSessionId) {
        return;
      }
      setSessionError(null);
      setConfigBusyId(configId);
      try {
        const refreshed = await client.setSessionConfigOption(
          activeSessionId,
          configId,
          value,
        );
        setSessionMetaById((prev) => {
          const base = prev[activeSessionId] ?? EMPTY_SESSION_META;
          const nextOptions =
            refreshed.length > 0
              ? refreshed
              : base.configOptions.map((opt) =>
                  opt.id === configId ? { ...opt, currentValue: value } : opt,
                );
          return {
            ...prev,
            [activeSessionId]: {
              ...base,
              configOptions: nextOptions,
              modes: syncModesWithOptions(base.modes, nextOptions),
            },
          };
        });
      } catch (err: unknown) {
        setSessionError(
          err instanceof Error
            ? err.message
            : `Failed to update '${configId}'.`,
        );
      } finally {
        setConfigBusyId(null);
      }
    },
    [activeSessionId],
  );

  const setSessionMode = useCallback(
    async (modeId: string) => {
      const client = clientRef.current;
      if (!client || !activeSessionId) {
        return;
      }
      setSessionError(null);
      setConfigBusyId("mode");
      try {
        await client.setSessionMode(activeSessionId, modeId);
        setSessionMetaById((prev) => {
          const base = prev[activeSessionId] ?? EMPTY_SESSION_META;
          const nextModes: AcpModesState = base.modes
            ? { ...base.modes, currentModeId: modeId }
            : { currentModeId: modeId, availableModes: [] };
          const nextOptions = base.configOptions.map((opt) =>
            opt.category === "mode" || opt.id === "mode"
              ? { ...opt, currentValue: modeId }
              : opt,
          );
          return {
            ...prev,
            [activeSessionId]: {
              ...base,
              modes: nextModes,
              configOptions: nextOptions,
            },
          };
        });
      } catch (err: unknown) {
        setSessionError(
          err instanceof Error ? err.message : "Failed to switch session mode.",
        );
      } finally {
        setConfigBusyId(null);
      }
    },
    [activeSessionId],
  );

  const closeActiveSession = useCallback(async () => {
    const client = clientRef.current;
    if (!client || !activeSessionId) {
      return;
    }
    const closingId = activeSessionId;
    setSessionError(null);
    setSessionBusyId("close");
    try {
      await client.closeSession(closingId);
      setActiveSessionMode(null);
      resetTimelineState(null);
      setSessionMetaById((prev) => {
        const next = { ...prev };
        delete next[closingId];
        return next;
      });
      setSessionArtifacts([]);
      setSelectedArtifact(null);
    } catch (err: unknown) {
      setSessionError(
        err instanceof Error ? err.message : "Failed to close session.",
      );
    } finally {
      setSessionBusyId(null);
    }
  }, [activeSessionId, resetTimelineState]);

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
        setSessionMetaById((prev) => {
          const next = { ...prev };
          delete next[sessionId];
          return next;
        });
        if (activeSessionId === sessionId) {
          setActiveSessionMode(null);
          resetTimelineState(null);
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
    [activeSessionId, resetTimelineState],
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
  const activeMeta = activeSessionId
    ? (sessionMetaById[activeSessionId] ?? EMPTY_SESSION_META)
    : EMPTY_SESSION_META;

  return {
    status,
    result,
    authStatus,
    activeMethodId,
    authenticatingMethodId,
    canLogout,
    accountTier,
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
    hasMoreMessages,
    loadingMessages,
    loadingOlderMessages,
    isLiveThinking,
    totalMessageItems,
    loadOlderMessages,
    sessionModels: activeMeta.models,
    sessionModes: activeMeta.modes,
    configOptions: activeMeta.configOptions,
    availableCommands: activeMeta.availableCommands,
    sessionUsage: activeMeta.usage,
    configBusyId,
    sessionArtifacts,
    selectedArtifact,
    artifactsBusy,
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
    resumeExistingSession,
    setSessionConfigOption,
    setSessionMode,
    closeActiveSession,
    deleteExistingSession,
    reconnect,
  };
}
