import { useState } from "react";
import {
  ArrowLeft,
  Database,
  FileText,
  FolderOpen,
  History,
  Loader2,
  MessageSquare,
  Play,
  Plus,
  RefreshCw,
  Trash2,
  Wrench,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import type {
  AcpReplayedMessage,
  AcpSessionArtifactContent,
  AcpSessionArtifactSummary,
  AcpSessionCapabilitiesFlags,
  AcpSessionInfo,
} from "@/lib/acp/types";

interface AcpSessionsPanelProps {
  defaultCwd: string;
  agyProfileDir: string;
  includeCbiMcp: boolean;
  onToggleCbiMcp: (enabled: boolean) => void;
  sessions: AcpSessionInfo[];
  nextCursor: string | null;
  activeSessionId: string | null;
  replayedMessages: AcpReplayedMessage[];
  sessionArtifacts: AcpSessionArtifactSummary[];
  selectedArtifact: AcpSessionArtifactContent | null;
  artifactsBusy: boolean;
  sessionCaps: AcpSessionCapabilitiesFlags;
  sessionBusyId: string | null;
  sessionError: string | null;
  onClearSessionError: () => void;
  onRefresh: () => Promise<void>;
  onLoadMore: () => Promise<void>;
  onCreateSession: () => Promise<void>;
  onLoadSession: (session: AcpSessionInfo) => Promise<void>;
  onResumeSession: (session: AcpSessionInfo) => Promise<void>;
  onCloseSession: () => Promise<void>;
  onDeleteSession: (sessionId: string) => Promise<void>;
  onRefreshArtifacts: () => Promise<void>;
  onOpenArtifact: (artifactName: string) => Promise<void>;
  onCloseArtifact: () => void;
}

function formatUpdatedAt(iso: string | null | undefined): string {
  if (!iso) {
    return "";
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatByteSize(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  return `${(bytes / 1024).toFixed(1)} KB`;
}

export function AcpSessionsPanel({
  defaultCwd,
  agyProfileDir,
  includeCbiMcp,
  onToggleCbiMcp,
  sessions,
  nextCursor,
  activeSessionId,
  replayedMessages,
  sessionArtifacts,
  selectedArtifact,
  artifactsBusy,
  sessionCaps,
  sessionBusyId,
  sessionError,
  onClearSessionError,
  onRefresh,
  onLoadMore,
  onCreateSession,
  onLoadSession,
  onResumeSession,
  onCloseSession,
  onDeleteSession,
  onRefreshArtifacts,
  onOpenArtifact,
  onCloseArtifact,
}: AcpSessionsPanelProps) {
  const [activeTab, setActiveTab] = useState<
    "sessions" | "messages" | "artifacts"
  >("sessions");

  const activeSession = sessions.find(
    (item) => item.sessionId === activeSessionId,
  );

  const handleLoadClick = async (session: AcpSessionInfo) => {
    await onLoadSession(session);
    setActiveTab("messages");
  };

  return (
    <aside className="flex h-full w-96 shrink-0 flex-col border-l border-border bg-card/50 text-xs">
      <div className="flex flex-col gap-2 border-b border-border p-3">
        <div className="flex items-center justify-between gap-2">
          <div className="min-w-0 flex-1">
            <div
              className="flex min-w-0 items-center gap-1.5 text-muted-foreground"
              title={defaultCwd}
            >
              <FolderOpen className="size-3.5 shrink-0" />
              <span className="truncate font-medium text-foreground">
                {defaultCwd || "Workspace"}
              </span>
            </div>
            <div
              className="truncate pl-5 text-[10px] text-muted-foreground"
              title={agyProfileDir}
            >
              Profile: {agyProfileDir}
            </div>
          </div>
          <button
            type="button"
            onClick={() => onToggleCbiMcp(!includeCbiMcp)}
            className={`inline-flex shrink-0 items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] font-medium transition-colors ${
              includeCbiMcp
                ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                : "border-border bg-muted/40 text-muted-foreground"
            }`}
            title="Attach server-curated codebase-indexer MCP server on session setup"
          >
            <Database className="size-3" />
            CBI MCP {includeCbiMcp ? "ON" : "OFF"}
          </button>
        </div>

        <div className="flex items-center gap-1.5">
          <Button
            size="sm"
            className="flex-1"
            disabled={!defaultCwd || sessionBusyId !== null}
            onClick={() => void onCreateSession()}
          >
            {sessionBusyId === "new" ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <Plus className="size-3.5" />
            )}
            New Session
          </Button>
          {sessionCaps.canList && (
            <Button
              variant="outline"
              size="icon-sm"
              disabled={sessionBusyId !== null}
              onClick={() => void onRefresh()}
              title="Refresh sessions"
            >
              <RefreshCw
                className={`size-3.5 ${sessionBusyId === "list" ? "animate-spin" : ""}`}
              />
            </Button>
          )}
        </div>

        {activeSessionId && (
          <div className="flex items-center justify-between gap-2 rounded-md border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-1.5">
            <div className="min-w-0">
              <div className="truncate font-medium text-foreground">
                {activeSession?.title || "Active session"}
              </div>
              <div className="truncate text-[10px] text-muted-foreground">
                {activeSessionId}
              </div>
            </div>
            {sessionCaps.canClose && (
              <Button
                variant="outline"
                size="xs"
                disabled={sessionBusyId === "close"}
                onClick={() => void onCloseSession()}
              >
                Close
              </Button>
            )}
          </div>
        )}

        {sessionError && (
          <div className="flex items-center justify-between gap-1.5 rounded-md border border-destructive/40 bg-destructive/10 px-2 py-1 text-destructive">
            <span className="truncate" title={sessionError}>
              {sessionError}
            </span>
            <button
              type="button"
              onClick={onClearSessionError}
              className="shrink-0 rounded p-0.5 hover:bg-destructive/20"
              aria-label="Dismiss session error"
            >
              <X className="size-3" />
            </button>
          </div>
        )}

        <div className="flex rounded-md border border-border bg-muted/40 p-0.5">
          <button
            type="button"
            onClick={() => setActiveTab("sessions")}
            className={`flex flex-1 items-center justify-center gap-1 rounded py-1 font-medium transition-colors ${
              activeTab === "sessions"
                ? "bg-background text-foreground shadow-xs"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            <History className="size-3" />
            Sessions ({sessions.length})
          </button>
          <button
            type="button"
            onClick={() => setActiveTab("messages")}
            className={`flex flex-1 items-center justify-center gap-1 rounded py-1 font-medium transition-colors ${
              activeTab === "messages"
                ? "bg-background text-foreground shadow-xs"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            <MessageSquare className="size-3" />
            History ({replayedMessages.length})
          </button>
          <button
            type="button"
            onClick={() => setActiveTab("artifacts")}
            className={`flex flex-1 items-center justify-center gap-1 rounded py-1 font-medium transition-colors ${
              activeTab === "artifacts"
                ? "bg-background text-foreground shadow-xs"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            <FileText className="size-3" />
            Artifacts ({sessionArtifacts.length})
          </button>
        </div>
      </div>

      {activeTab === "sessions" && (
        <ScrollArea className="min-h-0 flex-1">
          <div className="flex flex-col gap-2 p-3">
            {sessions.length === 0 ? (
              <div className="py-8 text-center text-muted-foreground">
                No sessions found for this workspace yet.
              </div>
            ) : (
              sessions.map((session) => {
                const isActive = session.sessionId === activeSessionId;
                const isBusy = sessionBusyId === session.sessionId;
                return (
                  <div
                    key={session.sessionId}
                    className={`flex flex-col gap-2 rounded-lg border p-2.5 transition-colors ${
                      isActive
                        ? "border-primary/50 bg-accent/40"
                        : "border-border bg-background/60"
                    }`}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0 flex-1">
                        <div
                          className="truncate font-medium text-foreground"
                          title={session.title || session.sessionId}
                        >
                          {session.title || "Untitled session"}
                        </div>
                        <div className="mt-0.5 flex items-center gap-1.5 text-[10px] text-muted-foreground">
                          <span className="truncate">
                            {session.sessionId.slice(0, 12)}
                          </span>
                          {session.updatedAt && (
                            <>
                              <span>·</span>
                              <span className="shrink-0">
                                {formatUpdatedAt(session.updatedAt)}
                              </span>
                            </>
                          )}
                        </div>
                      </div>
                    </div>

                    <div className="flex items-center justify-end gap-1.5">
                      {sessionCaps.canResume && (
                        <Button
                          variant="ghost"
                          size="xs"
                          disabled={sessionBusyId !== null}
                          onClick={() => void onResumeSession(session)}
                          title="Resume session without history replay (session/resume)"
                        >
                          <Play className="size-3" />
                          Resume
                        </Button>
                      )}
                      <Button
                        variant="outline"
                        size="xs"
                        disabled={!sessionCaps.canLoad || sessionBusyId !== null}
                        onClick={() => void handleLoadClick(session)}
                        title="Load session and replay conversation history (session/load)"
                      >
                        {isBusy ? (
                          <Loader2 className="size-3 animate-spin" />
                        ) : null}
                        Load
                      </Button>
                      <Button
                        variant="destructive"
                        size="xs"
                        disabled={
                          !sessionCaps.canDelete || sessionBusyId !== null
                        }
                        onClick={() => void onDeleteSession(session.sessionId)}
                        title={
                          sessionCaps.canDelete
                            ? "Delete session from history (session/delete)"
                            : "Agent did not advertise sessionCapabilities.delete"
                        }
                      >
                        <Trash2 className="size-3" />
                        Delete
                      </Button>
                    </div>
                  </div>
                );
              })
            )}

            {nextCursor && (
              <Button
                variant="outline"
                size="sm"
                disabled={sessionBusyId !== null}
                onClick={() => void onLoadMore()}
              >
                {sessionBusyId === "more" ? (
                  <Loader2 className="size-3.5 animate-spin" />
                ) : null}
                Load more
              </Button>
            )}
          </div>
        </ScrollArea>
      )}

      {activeTab === "messages" && (
        <ScrollArea className="min-h-0 flex-1">
          <div className="flex flex-col gap-2 p-3">
            {replayedMessages.length === 0 ? (
              <div className="py-8 text-center text-muted-foreground">
                {activeSessionId
                  ? "No replayed messages in this session yet."
                  : "Load a session to replay its conversation history."}
              </div>
            ) : (
              replayedMessages.map((msg, idx) =>
                msg.role === "tool" ? (
                  <div
                    key={`${msg.id}-${idx}`}
                    className="flex items-center justify-between gap-2 rounded-md border border-border/70 bg-muted/30 px-2.5 py-1.5"
                  >
                    <div className="flex min-w-0 items-center gap-1.5">
                      <Wrench className="size-3 shrink-0 text-muted-foreground" />
                      <span
                        className="truncate font-medium text-foreground"
                        title={msg.text}
                      >
                        {msg.text}
                      </span>
                    </div>
                    <div className="flex shrink-0 items-center gap-1 text-[10px] text-muted-foreground">
                      {msg.toolKind && (
                        <span className="rounded bg-background/80 px-1.5 py-0.5">
                          {msg.toolKind}
                        </span>
                      )}
                      {msg.toolStatus && (
                        <span className="rounded bg-background/80 px-1.5 py-0.5 uppercase">
                          {msg.toolStatus}
                        </span>
                      )}
                    </div>
                  </div>
                ) : (
                  <div
                    key={`${msg.id}-${idx}`}
                    className={`rounded-lg border p-2.5 ${
                      msg.role === "user"
                        ? "border-border bg-muted/50"
                        : msg.role === "thought"
                          ? "border-border/60 bg-background/40 italic text-muted-foreground"
                          : "border-border bg-background"
                    }`}
                  >
                    <div className="mb-1 text-[10px] font-semibold tracking-wide text-muted-foreground uppercase">
                      {msg.role}
                    </div>
                    <pre className="font-sans text-xs whitespace-pre-wrap wrap-break-word text-foreground">
                      {msg.text}
                    </pre>
                  </div>
                ),
              )
            )}
          </div>
        </ScrollArea>
      )}

      {activeTab === "artifacts" && (
        <ScrollArea className="min-h-0 flex-1">
          <div className="flex flex-col gap-2 p-3">
            {!activeSessionId ? (
              <div className="py-8 text-center text-muted-foreground">
                Load or resume a session to inspect its markdown artifacts.
              </div>
            ) : selectedArtifact ? (
              <div className="flex flex-col gap-2">
                <div className="flex items-center justify-between gap-2">
                  <Button
                    variant="ghost"
                    size="xs"
                    onClick={onCloseArtifact}
                  >
                    <ArrowLeft className="size-3" />
                    All artifacts
                  </Button>
                  <span className="text-[10px] text-muted-foreground">
                    {formatByteSize(selectedArtifact.size_bytes)}
                  </span>
                </div>
                <div className="rounded-lg border border-border bg-background p-2.5">
                  <div className="font-medium text-foreground">
                    {selectedArtifact.name}
                  </div>
                  {selectedArtifact.summary && (
                    <div className="mt-1 text-[11px] text-muted-foreground">
                      {selectedArtifact.summary}
                    </div>
                  )}
                  <pre className="mt-2 border-t border-border pt-2 font-mono text-[11px] leading-relaxed whitespace-pre-wrap wrap-break-word text-foreground">
                    {selectedArtifact.content}
                  </pre>
                </div>
              </div>
            ) : (
              <>
                <div className="flex items-center justify-between">
                  <span className="text-[11px] text-muted-foreground">
                    brain/{activeSessionId.slice(0, 12)}/*.md
                  </span>
                  <Button
                    variant="ghost"
                    size="xs"
                    disabled={artifactsBusy}
                    onClick={() => void onRefreshArtifacts()}
                  >
                    <RefreshCw
                      className={`size-3 ${artifactsBusy ? "animate-spin" : ""}`}
                    />
                    Refresh
                  </Button>
                </div>
                {sessionArtifacts.length === 0 ? (
                  <div className="py-6 text-center text-muted-foreground">
                    {artifactsBusy
                      ? "Loading session artifacts…"
                      : "No markdown artifacts found for this session."}
                  </div>
                ) : (
                  sessionArtifacts.map((art) => (
                    <button
                      key={art.name}
                      type="button"
                      disabled={artifactsBusy}
                      onClick={() => void onOpenArtifact(art.name)}
                      className="flex flex-col gap-1 rounded-lg border border-border bg-background/60 p-2.5 text-left transition-colors hover:border-primary/50 hover:bg-accent/30"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="flex min-w-0 items-center gap-1.5 truncate font-medium text-foreground">
                          <FileText className="size-3.5 shrink-0 text-muted-foreground" />
                          <span className="truncate">{art.name}</span>
                        </span>
                        <span className="shrink-0 text-[10px] text-muted-foreground">
                          {formatByteSize(art.size_bytes)}
                        </span>
                      </div>
                      {art.summary && (
                        <div className="line-clamp-2 text-[11px] text-muted-foreground">
                          {art.summary}
                        </div>
                      )}
                      {art.updated_at && (
                        <div className="text-[10px] text-muted-foreground">
                          {formatUpdatedAt(art.updated_at)}
                        </div>
                      )}
                    </button>
                  ))
                )}
              </>
            )}
          </div>
        </ScrollArea>
      )}
    </aside>
  );
}
