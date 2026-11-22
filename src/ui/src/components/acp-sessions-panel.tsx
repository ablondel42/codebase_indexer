import { useMemo, useState } from "react";
import {
  ArchiveIcon,
  ArchiveRestoreIcon,
  ArrowLeftIcon,
  CircleAlertIcon,
  DatabaseIcon,
  FileTextIcon,
  FolderOpenIcon,
  HistoryIcon,
  MessageSquareIcon,
  PlayIcon,
  PlusIcon,
  RefreshCwIcon,
  XIcon,
} from "lucide-react";

import { SessionMessageBody } from "@/components/ai-elements/message-body";
import { SessionChat } from "@/components/ai-elements/session-chat";
import { FileTree } from "@/components/ai-elements/turn-prompts-and-files";
import {
  Alert,
  AlertAction,
  AlertDescription,
  AlertTitle,
} from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ButtonGroup } from "@/components/ui/button-group";
import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty";
import {
  Item,
  ItemActions,
  ItemContent,
  ItemDescription,
  ItemFooter,
  ItemGroup,
  ItemMedia,
  ItemTitle,
} from "@/components/ui/item";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { Spinner } from "@/components/ui/spinner";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Toggle } from "@/components/ui/toggle";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useArchivedSessions } from "@/hooks/use-archived-sessions";
import type {
  AcpActiveSessionMode,
  AcpAvailableCommand,
  AcpConfigOption,
  AcpModelsState,
  AcpModesState,
  AcpReplayedMessage,
  AcpSessionArtifactContent,
  AcpSessionArtifactSummary,
  AcpSessionCapabilitiesFlags,
  AcpSessionInfo,
  AcpSessionUsage,
} from "@/lib/acp/types";

type SessionsPanelTab = "sessions" | "chat" | "artifacts";

interface AcpSessionsPanelProps {
  defaultCwd: string;
  agyProfileDir: string;
  includeCbiMcp: boolean;
  onToggleCbiMcp: (enabled: boolean) => void;
  sessions: AcpSessionInfo[];
  nextCursor: string | null;
  activeSessionId: string | null;
  activeSessionMode: AcpActiveSessionMode;
  replayedMessages: AcpReplayedMessage[];
  sessionModels: AcpModelsState | null;
  sessionModes: AcpModesState | null;
  configOptions: AcpConfigOption[];
  availableCommands: AcpAvailableCommand[];
  sessionUsage: AcpSessionUsage | null;
  configBusyId: string | null;
  onSetConfigOption: (
    configId: string,
    value: string | boolean,
  ) => Promise<void>;
  onSetMode: (modeId: string) => Promise<void>;
  sessionArtifacts: AcpSessionArtifactSummary[];
  selectedArtifact: AcpSessionArtifactContent | null;
  artifactsBusy: boolean;
  onLoadArtifactContent: (
    artifactName: string,
  ) => Promise<AcpSessionArtifactContent | null>;
  sessionCaps: AcpSessionCapabilitiesFlags;
  sessionBusyId: string | null;
  sessionError: string | null;
  onClearSessionError: () => void;
  onRefresh: () => Promise<void>;
  onLoadMore: () => Promise<void>;
  onCreateSession: () => Promise<void>;
  onResumeSession: (session: AcpSessionInfo) => Promise<void>;
  onCloseSession: () => Promise<void>;
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
  activeSessionMode,
  replayedMessages,
  sessionModels,
  sessionModes,
  configOptions,
  availableCommands,
  sessionUsage,
  configBusyId,
  onSetConfigOption,
  onSetMode,
  sessionArtifacts,
  selectedArtifact,
  artifactsBusy,
  onLoadArtifactContent,
  sessionCaps,
  sessionBusyId,
  sessionError,
  onClearSessionError,
  onRefresh,
  onLoadMore,
  onCreateSession,
  onResumeSession,
  onCloseSession,
  onRefreshArtifacts,
  onOpenArtifact,
  onCloseArtifact,
}: AcpSessionsPanelProps) {
  const [activeTab, setActiveTab] = useState<SessionsPanelTab>("sessions");
  const {
    showArchived,
    setShowArchived,
    isArchived,
    toggleArchived,
    countArchivedIn,
  } = useArchivedSessions();

  const activeSession = sessions.find(
    (item) => item.sessionId === activeSessionId,
  );
  const archivedCount = useMemo(
    () => countArchivedIn(sessions),
    [countArchivedIn, sessions],
  );
  const visibleSessions = useMemo(
    () =>
      showArchived
        ? sessions
        : sessions.filter((item) => !isArchived(item.sessionId)),
    [isArchived, sessions, showArchived],
  );
  const artifactPaths = useMemo(
    () => sessionArtifacts.map((art) => art.name),
    [sessionArtifacts],
  );

  const canResumeAny = sessionCaps.canLoad || sessionCaps.canResume;

  const handleResumeClick = async (session: AcpSessionInfo) => {
    await onResumeSession(session);
    setActiveTab("chat");
  };

  const handleCreateClick = async () => {
    await onCreateSession();
    setActiveTab("chat");
  };

  return (
    <div className="flex size-full min-h-0 min-w-0 flex-col bg-card/40">
      <div className="flex shrink-0 flex-col gap-2.5 p-3 pb-2">
        <Item variant="muted" size="xs">
          <ItemMedia variant="icon">
            <FolderOpenIcon />
          </ItemMedia>
          <ItemContent className="min-w-0">
            <ItemTitle className="max-w-full">
              <span className="truncate" title={defaultCwd}>
                {defaultCwd || "Workspace"}
              </span>
            </ItemTitle>
            <ItemDescription className="truncate" title={agyProfileDir}>
              Profile: {agyProfileDir}
            </ItemDescription>
          </ItemContent>
          <ItemActions>
            <Tooltip>
              <TooltipTrigger
                render={
                  <Toggle
                    variant="outline"
                    size="sm"
                    pressed={includeCbiMcp}
                    onPressedChange={(pressed) => onToggleCbiMcp(pressed)}
                    aria-label="Toggle codebase-indexer MCP attachment"
                  />
                }
              >
                <DatabaseIcon />
                <span>CBI MCP</span>
                <Badge variant={includeCbiMcp ? "default" : "outline"}>
                  {includeCbiMcp ? "ON" : "OFF"}
                </Badge>
              </TooltipTrigger>
              <TooltipContent>
                <p>
                  Attach server-curated codebase-indexer MCP server on session
                  setup
                </p>
              </TooltipContent>
            </Tooltip>
          </ItemActions>
        </Item>

        <div className="flex items-center justify-between gap-2">
          <ButtonGroup className="flex-1">
            <Button
              size="sm"
              className="flex-1"
              disabled={!defaultCwd || sessionBusyId !== null}
              onClick={() => void handleCreateClick()}
            >
              {sessionBusyId === "new" ? <Spinner /> : <PlusIcon />}
              New Session
            </Button>
            {sessionCaps.canList && (
              <Tooltip>
                <TooltipTrigger
                  render={
                    <Button
                      variant="outline"
                      size="icon-sm"
                      aria-label="Refresh sessions"
                      disabled={sessionBusyId !== null}
                      onClick={() => void onRefresh()}
                    />
                  }
                >
                  {sessionBusyId === "list" ? <Spinner /> : <RefreshCwIcon />}
                </TooltipTrigger>
                <TooltipContent>
                  <p>Refresh sessions list</p>
                </TooltipContent>
              </Tooltip>
            )}
          </ButtonGroup>

          <Tooltip>
            <TooltipTrigger
              render={
                <Toggle
                  variant="outline"
                  size="sm"
                  pressed={showArchived}
                  onPressedChange={setShowArchived}
                  aria-label={
                    showArchived
                      ? "Hide archived conversations"
                      : "Show archived conversations"
                  }
                />
              }
            >
              <ArchiveIcon />
              <span>Archived</span>
              {archivedCount > 0 && (
                <Badge variant="secondary">{archivedCount}</Badge>
              )}
            </TooltipTrigger>
            <TooltipContent>
              <p>
                {showArchived
                  ? "Hide archived conversations"
                  : "Show archived conversations"}
              </p>
            </TooltipContent>
          </Tooltip>
        </div>

        {sessionError && (
          <Alert variant="destructive">
            <CircleAlertIcon />
            <AlertTitle>Session error</AlertTitle>
            <AlertDescription className="truncate" title={sessionError}>
              {sessionError}
            </AlertDescription>
            <AlertAction>
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label="Dismiss session error"
                onClick={onClearSessionError}
              >
                <XIcon />
              </Button>
            </AlertAction>
          </Alert>
        )}
      </div>

      <Tabs
        value={activeTab}
        onValueChange={(value) => setActiveTab(value as SessionsPanelTab)}
        className="min-h-0 flex-1 gap-0"
      >
        <div className="shrink-0 px-3 pb-2">
          <TabsList className="w-full">
            <TabsTrigger value="sessions">
              <HistoryIcon />
              <span>Sessions</span>
              <Badge variant="secondary">{visibleSessions.length}</Badge>
            </TabsTrigger>
            <TabsTrigger value="chat">
              <MessageSquareIcon />
              <span>Chat</span>
              {replayedMessages.length > 0 && (
                <Badge variant="secondary">{replayedMessages.length}</Badge>
              )}
            </TabsTrigger>
            <TabsTrigger value="artifacts">
              <FileTextIcon />
              <span>Artifacts</span>
              {sessionArtifacts.length > 0 && (
                <Badge variant="secondary">{sessionArtifacts.length}</Badge>
              )}
            </TabsTrigger>
          </TabsList>
        </div>

        <Separator />

        <TabsContent value="sessions" className="flex min-h-0 flex-1 flex-col">
          <ScrollArea className="min-h-0 flex-1">
            <div className="flex flex-col gap-2.5 p-3">
              {visibleSessions.length === 0 ? (
                <Empty className="py-10">
                  <EmptyHeader>
                    <EmptyMedia variant="icon">
                      {sessions.length > 0 && archivedCount > 0 ? (
                        <ArchiveIcon />
                      ) : (
                        <HistoryIcon />
                      )}
                    </EmptyMedia>
                    <EmptyTitle>
                      {sessions.length > 0 && archivedCount > 0
                        ? "All conversations are archived"
                        : "No sessions yet"}
                    </EmptyTitle>
                    <EmptyDescription>
                      {sessions.length > 0 && archivedCount > 0
                        ? `${archivedCount} archived ${archivedCount === 1 ? "conversation is" : "conversations are"} hidden from the active list.`
                        : "Start a new ACP session in this workspace to inspect and chat with the agent."}
                    </EmptyDescription>
                  </EmptyHeader>
                  <EmptyContent className="flex-row justify-center gap-2">
                    <Button
                      size="sm"
                      disabled={!defaultCwd || sessionBusyId !== null}
                      onClick={() => void handleCreateClick()}
                    >
                      {sessionBusyId === "new" ? <Spinner /> : <PlusIcon />}
                      New Session
                    </Button>
                    {sessions.length > 0 &&
                      archivedCount > 0 &&
                      !showArchived && (
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => setShowArchived(true)}
                        >
                          <ArchiveRestoreIcon />
                          Show Archived ({archivedCount})
                        </Button>
                      )}
                  </EmptyContent>
                </Empty>
              ) : (
                <ItemGroup data-size="xs" className="gap-2">
                  {visibleSessions.map((session) => {
                    const isActive = session.sessionId === activeSessionId;
                    const isBusy = sessionBusyId === session.sessionId;
                    const archived = isArchived(session.sessionId);

                    return (
                      <Item
                        key={session.sessionId}
                        variant={isActive ? "muted" : "outline"}
                        size="xs"
                        className={archived ? "opacity-75" : undefined}
                      >
                        <ItemContent className="min-w-0">
                          <ItemTitle className="max-w-full">
                            <span
                              className="truncate"
                              title={session.title || session.sessionId}
                            >
                              {session.title || "Untitled session"}
                            </span>
                            {isActive && (
                              <Badge variant="default">Active</Badge>
                            )}
                            {archived && (
                              <Badge variant="outline">Archived</Badge>
                            )}
                          </ItemTitle>
                          <ItemDescription className="truncate">
                            {session.sessionId.slice(0, 12)}
                            {session.updatedAt
                              ? ` · ${formatUpdatedAt(session.updatedAt)}`
                              : ""}
                          </ItemDescription>
                        </ItemContent>
                        <ItemFooter className="justify-end pt-1">
                          <ButtonGroup>
                            <Tooltip>
                              <TooltipTrigger
                                render={
                                  <Button
                                    variant="outline"
                                    size="xs"
                                    disabled={
                                      !canResumeAny || sessionBusyId !== null
                                    }
                                    onClick={() =>
                                      void handleResumeClick(session)
                                    }
                                  />
                                }
                              >
                                {isBusy ? <Spinner /> : <PlayIcon />}
                                Resume
                              </TooltipTrigger>
                              <TooltipContent>
                                <p>
                                  {sessionCaps.canLoad
                                    ? "Resume session and replay conversation history"
                                    : "Resume session"}
                                </p>
                              </TooltipContent>
                            </Tooltip>
                            <Tooltip>
                              <TooltipTrigger
                                render={
                                  <Button
                                    variant="outline"
                                    size="xs"
                                    aria-label={
                                      archived
                                        ? "Unarchive conversation"
                                        : "Archive conversation"
                                    }
                                    onClick={() =>
                                      toggleArchived(session.sessionId)
                                    }
                                  />
                                }
                              >
                                {archived ? (
                                  <ArchiveRestoreIcon />
                                ) : (
                                  <ArchiveIcon />
                                )}
                                {archived ? "Unarchive" : "Archive"}
                              </TooltipTrigger>
                              <TooltipContent>
                                <p>
                                  {archived
                                    ? "Restore conversation to main sessions list"
                                    : "Archive conversation locally (hides unless Archived filter is active)"}
                                </p>
                              </TooltipContent>
                            </Tooltip>
                          </ButtonGroup>
                        </ItemFooter>
                      </Item>
                    );
                  })}
                </ItemGroup>
              )}

              {nextCursor && (
                <Button
                  variant="outline"
                  size="sm"
                  disabled={sessionBusyId !== null}
                  onClick={() => void onLoadMore()}
                >
                  {sessionBusyId === "more" ? <Spinner /> : null}
                  Load more
                </Button>
              )}
            </div>
          </ScrollArea>
        </TabsContent>

        <TabsContent value="chat" className="flex min-h-0 flex-1 flex-col">
          <SessionChat
            activeSessionId={activeSessionId}
            activeSession={activeSession}
            activeSessionMode={activeSessionMode}
            replayedMessages={replayedMessages}
            sessionModels={sessionModels}
            sessionModes={sessionModes}
            configOptions={configOptions}
            availableCommands={availableCommands}
            sessionUsage={sessionUsage}
            configBusyId={configBusyId}
            onSetConfigOption={onSetConfigOption}
            onSetMode={onSetMode}
            sessionArtifacts={sessionArtifacts}
            onLoadArtifact={onLoadArtifactContent}
            includeCbiMcp={includeCbiMcp}
            onToggleCbiMcp={onToggleCbiMcp}
            sessionCaps={sessionCaps}
            sessionBusyId={sessionBusyId}
            onCreateSession={handleCreateClick}
            onCloseSession={onCloseSession}
            onSwitchToSessionsTab={() => setActiveTab("sessions")}
          />
        </TabsContent>

        <TabsContent value="artifacts" className="flex min-h-0 flex-1 flex-col">
          <ScrollArea className="min-h-0 flex-1">
            <div className="flex flex-col gap-3 p-3">
              {!activeSessionId ? (
                <Empty className="py-10">
                  <EmptyHeader>
                    <EmptyMedia variant="icon">
                      <FileTextIcon />
                    </EmptyMedia>
                    <EmptyTitle>No session selected</EmptyTitle>
                    <EmptyDescription>
                      Resume a session to browse its markdown artifacts.
                    </EmptyDescription>
                  </EmptyHeader>
                  <EmptyContent>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => setActiveTab("sessions")}
                    >
                      <HistoryIcon />
                      Browse Sessions
                    </Button>
                  </EmptyContent>
                </Empty>
              ) : selectedArtifact ? (
                <div className="flex flex-col gap-2.5">
                  <div className="flex items-center justify-between gap-2">
                    <Button
                      variant="outline"
                      size="xs"
                      onClick={onCloseArtifact}
                    >
                      <ArrowLeftIcon />
                      All artifacts
                    </Button>
                    <Badge variant="secondary">
                      {formatByteSize(selectedArtifact.size_bytes)}
                    </Badge>
                  </div>
                  <Item variant="outline" size="sm">
                    <ItemContent className="min-w-0">
                      <ItemTitle>{selectedArtifact.name}</ItemTitle>
                      {selectedArtifact.summary && (
                        <ItemDescription>
                          {selectedArtifact.summary}
                        </ItemDescription>
                      )}
                    </ItemContent>
                  </Item>
                  <div className="rounded-lg border border-border bg-background/60 p-3">
                    <SessionMessageBody text={selectedArtifact.content} />
                  </div>
                </div>
              ) : (
                <>
                  <div className="flex items-center justify-between gap-2">
                    <Badge variant="outline" className="font-mono">
                      brain/{activeSessionId.slice(0, 12)}/*.md
                    </Badge>
                    <Button
                      variant="outline"
                      size="xs"
                      disabled={artifactsBusy}
                      onClick={() => void onRefreshArtifacts()}
                    >
                      {artifactsBusy ? <Spinner /> : <RefreshCwIcon />}
                      Refresh
                    </Button>
                  </div>

                  {sessionArtifacts.length === 0 ? (
                    <Empty className="py-8">
                      <EmptyHeader>
                        <EmptyMedia variant="icon">
                          {artifactsBusy ? <Spinner /> : <FileTextIcon />}
                        </EmptyMedia>
                        <EmptyTitle>
                          {artifactsBusy
                            ? "Loading artifacts…"
                            : "No markdown artifacts"}
                        </EmptyTitle>
                        <EmptyDescription>
                          Plans, walkthroughs, and reports created during this
                          session appear here.
                        </EmptyDescription>
                      </EmptyHeader>
                    </Empty>
                  ) : (
                    <>
                      <FileTree
                        paths={artifactPaths}
                        onSelect={(path) => void onOpenArtifact(path)}
                      />
                      <ItemGroup data-size="xs" className="gap-2">
                        {sessionArtifacts.map((art) => (
                          <Item
                            key={art.name}
                            variant="outline"
                            size="xs"
                            render={
                              <button
                                type="button"
                                disabled={artifactsBusy}
                                onClick={() => void onOpenArtifact(art.name)}
                                className="cursor-pointer text-left hover:bg-muted/50"
                              />
                            }
                          >
                            <ItemMedia variant="icon">
                              <FileTextIcon />
                            </ItemMedia>
                            <ItemContent className="min-w-0">
                              <ItemTitle className="w-full justify-between">
                                <span className="truncate">{art.name}</span>
                                <Badge variant="secondary">
                                  {formatByteSize(art.size_bytes)}
                                </Badge>
                              </ItemTitle>
                              {art.summary && (
                                <ItemDescription>{art.summary}</ItemDescription>
                              )}
                            </ItemContent>
                          </Item>
                        ))}
                      </ItemGroup>
                    </>
                  )}
                </>
              )}
            </div>
          </ScrollArea>
        </TabsContent>
      </Tabs>
    </div>
  );
}
