import * as React from "react";
import {
  HistoryIcon,
  MessageCircleDashedIcon,
  PlusIcon,
  XIcon,
} from "lucide-react";

import {
  PlanRow,
  SubagentRow,
  TaskRow,
  TodoQueue,
} from "@/components/ai-elements/agent-workflow-trace";
import {
  AssistantMessageFooter,
  SessionMessageBody,
  SourcesList,
  UserMessageFooter,
} from "@/components/ai-elements/message-body";
import {
  PromptSuggestions,
  SessionPromptInput,
} from "@/components/ai-elements/prompt-composer";
import {
  ChainOfThoughtGroup,
  ReasoningGroup,
} from "@/components/ai-elements/reasoning-trace";
import { ToolRow } from "@/components/ai-elements/tool-trace";
import {
  CheckpointMarker,
  ContextUsageHoverCard,
  ToolConfirmationCard,
} from "@/components/ai-elements/turn-prompts-and-files";
import { Bubble, BubbleContent } from "@/components/ui/bubble";
import { Button } from "@/components/ui/button";
import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty";
import { Message, MessageContent } from "@/components/ui/message";
import {
  MessageScroller,
  MessageScrollerButton,
  MessageScrollerContent,
  MessageScrollerItem,
  MessageScrollerProvider,
  MessageScrollerViewport,
} from "@/components/ui/message-scroller";
import { Separator } from "@/components/ui/separator";
import { Spinner } from "@/components/ui/spinner";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Trace } from "@/components/ui/trace";
import {
  DEFAULT_PROMPT_SUGGESTIONS,
  estimateContextUsage,
  groupMessagesIntoTurns,
  isResearchStep,
} from "@/lib/acp/turn-grouping";
import type {
  AcpActiveSessionMode,
  AcpAvailableCommand,
  AcpConfigOption,
  AcpModelsState,
  AcpModesState,
  AcpReplayedMessage,
  AcpSessionCapabilitiesFlags,
  AcpSessionInfo,
  AcpSessionUsage,
} from "@/lib/acp/types";

function renderAssistantTraceSteps(steps: AcpReplayedMessage[]) {
  const nodes: React.ReactNode[] = [];
  let idx = 0;

  while (idx < steps.length) {
    const step = steps[idx]!;

    // Group >=2 consecutive read/search/fetch tools into ChainOfThoughtGroup
    if (isResearchStep(step)) {
      const batch: AcpReplayedMessage[] = [step];
      let nextIdx = idx + 1;
      while (nextIdx < steps.length && isResearchStep(steps[nextIdx]!)) {
        batch.push(steps[nextIdx]!);
        nextIdx += 1;
      }

      if (batch.length >= 2) {
        const files = Array.from(
          new Set(batch.flatMap((item) => item.locations ?? [])),
        );
        const failedCount = batch.filter(
          (item) => item.toolStatus === "error",
        ).length;
        const anyRunning = batch.some((item) => item.toolStatus === "running");

        nodes.push(
          <ChainOfThoughtGroup
            key={`cot-${step.id}`}
            stepCount={batch.length}
            failedCount={failedCount}
            running={anyRunning}
            files={files}
          >
            {batch.map((item) => (
              <ToolRow key={item.id} step={item} />
            ))}
          </ChainOfThoughtGroup>,
        );
        idx = nextIdx;
        continue;
      }
    }

    if (step.role === "plan" && step.planSteps && step.planSteps.length > 0) {
      nodes.push(
        <PlanRow
          key={step.id}
          title={step.planTitle || "Execution plan"}
          steps={step.planSteps}
        />,
      );
    } else if (step.task) {
      nodes.push(<TaskRow key={step.id} task={step.task} />);
    } else if (step.subagent) {
      nodes.push(<SubagentRow key={step.id} subagent={step.subagent} />);
    } else if (step.role === "tool") {
      nodes.push(<ToolRow key={step.id} step={step} />);
    }

    idx += 1;
  }

  return nodes;
}

const EMPTY_LIVE_ITEM_IDS: ReadonlySet<string> = new Set();

export interface SessionChatProps {
  activeSessionId: string | null;
  activeSession: AcpSessionInfo | undefined;
  activeSessionMode: AcpActiveSessionMode;
  replayedMessages: AcpReplayedMessage[];
  hasMoreMessages?: boolean;
  loadingMessages?: boolean;
  loadingOlderMessages?: boolean;
  isLiveThinking?: boolean;
  liveTurnActive?: boolean;
  liveTurnStartedAtMs?: number | null;
  liveItemIds?: ReadonlySet<string>;
  onLoadOlderMessages?: () => Promise<void>;
  sessionModels?: AcpModelsState | null;
  sessionModes?: AcpModesState | null;
  configOptions?: AcpConfigOption[];
  availableCommands?: AcpAvailableCommand[];
  sessionUsage?: AcpSessionUsage | null;
  configBusyId?: string | null;
  onSetConfigOption?: (
    configId: string,
    value: string | boolean,
  ) => Promise<void>;
  onSetMode?: (modeId: string) => Promise<void>;
  includeCbiMcp: boolean;
  onToggleCbiMcp: (enabled: boolean) => void;
  sessionCaps: AcpSessionCapabilitiesFlags;
  sessionBusyId: string | null;
  onCreateSession: () => Promise<void>;
  onCloseSession: () => Promise<void>;
  onSwitchToSessionsTab: () => void;
}

function SessionTimelineScroller({
  activeSessionMode,
  turns,
  hasMoreMessages,
  loadingMessages,
  loadingOlderMessages,
  isLiveThinking,
  liveTurnActive,
  liveTurnStartedAtMs,
  liveItemIds,
  onLoadOlderMessages,
}: {
  activeSessionMode: AcpActiveSessionMode;
  turns: ReturnType<typeof groupMessagesIntoTurns>;
  hasMoreMessages: boolean;
  loadingMessages: boolean;
  loadingOlderMessages: boolean;
  isLiveThinking: boolean;
  liveTurnActive: boolean;
  liveTurnStartedAtMs: number | null;
  liveItemIds: ReadonlySet<string>;
  onLoadOlderMessages?: () => Promise<void>;
}) {
  const [scrollbarPaused, setScrollbarPaused] = React.useState(false);
  const lastScrollTopRef = React.useRef(0);
  const draggingScrollbarRef = React.useRef(false);
  const settledInitialRef = React.useRef(false);

  React.useEffect(() => {
    if ((turns.length === 0 && !liveTurnActive) || loadingMessages) {
      return;
    }
    const rafId = window.requestAnimationFrame(() => {
      settledInitialRef.current = true;
    });
    return () => window.cancelAnimationFrame(rafId);
  }, [liveTurnActive, loadingMessages, turns.length]);

  const handleViewportPointerDown = (
    event: React.PointerEvent<HTMLDivElement>,
  ) => {
    const el = event.currentTarget;
    const scrollbarWidth = el.offsetWidth - el.clientWidth;
    if (scrollbarWidth <= 0) {
      return;
    }
    const rect = el.getBoundingClientRect();
    const isRtl = window.getComputedStyle(el).direction === "rtl";
    const onGutter = isRtl
      ? event.clientX <= rect.left + scrollbarWidth
      : event.clientX >= rect.left + el.clientWidth;
    if (!onGutter) {
      return;
    }
    draggingScrollbarRef.current = true;
    const clearDrag = () => {
      draggingScrollbarRef.current = false;
    };
    window.addEventListener("pointerup", clearDrag, { once: true });
    window.addEventListener("pointercancel", clearDrag, { once: true });
  };

  const handleViewportScroll = (event: React.UIEvent<HTMLDivElement>) => {
    const el = event.currentTarget;
    const prevTop = lastScrollTopRef.current;
    const currentTop = el.scrollTop;
    lastScrollTopRef.current = currentTop;

    const distFromBottom = el.scrollHeight - currentTop - el.clientHeight;
    if (draggingScrollbarRef.current && currentTop < prevTop - 1) {
      setScrollbarPaused(true);
    } else if (scrollbarPaused && distFromBottom <= 24) {
      setScrollbarPaused(false);
    }

    if (
      settledInitialRef.current &&
      hasMoreMessages &&
      !loadingMessages &&
      !loadingOlderMessages &&
      onLoadOlderMessages &&
      el.scrollHeight > el.clientHeight + 32 &&
      currentTop <= 160 &&
      currentTop < prevTop
    ) {
      void onLoadOlderMessages();
    }
  };

  const showPendingAssistant =
    liveTurnActive &&
    (turns.length === 0 || turns[turns.length - 1]?.kind === "user");

  return (
    <MessageScrollerProvider
      autoScroll={!scrollbarPaused}
      defaultScrollPosition="end"
      scrollEdgeThreshold={24}
    >
      <MessageScroller>
        <MessageScrollerViewport
          preserveScrollOnPrepend
          onPointerDown={handleViewportPointerDown}
          onScroll={handleViewportScroll}
        >
          <MessageScrollerContent className="gap-5 px-3 py-4">
            {hasMoreMessages && (
              <div className="flex items-center justify-center py-1">
                {loadingOlderMessages ? (
                  <span className="inline-flex items-center gap-1.5 text-[11px] text-muted-foreground">
                    <Spinner className="size-3" />
                    Loading earlier messages…
                  </span>
                ) : (
                  <Button
                    type="button"
                    variant="ghost"
                    size="xs"
                    className="text-[11px] text-muted-foreground"
                    onClick={() => void onLoadOlderMessages?.()}
                  >
                    Load earlier messages
                  </Button>
                )}
              </div>
            )}
            {activeSessionMode === "resumed" && (
              <CheckpointMarker label="Session resumed · prior history not replayed" />
            )}
            {turns.map((turn, turnIdx) => {
              if (turn.kind === "user") {
                return (
                  <React.Fragment key={turn.id}>
                    <MessageScrollerItem messageId={turn.id}>
                      <Message align="end">
                        <MessageContent className="gap-1.5">
                          <Bubble align="end" variant="muted">
                            <BubbleContent className="whitespace-pre-wrap text-xs">
                              {turn.text}
                            </BubbleContent>
                          </Bubble>
                          <UserMessageFooter text={turn.text} />
                        </MessageContent>
                      </Message>
                    </MessageScrollerItem>
                    {turn.checkpointLabel && (
                      <CheckpointMarker label={turn.checkpointLabel} />
                    )}
                  </React.Fragment>
                );
              }

              const isLastTurn = turnIdx === turns.length - 1;
              const isTurnActive =
                isLastTurn && (liveTurnActive || isLiveThinking);
              const isThinkingNow =
                isLastTurn &&
                (isLiveThinking ||
                  (liveTurnActive && !turn.replyText && turn.thoughts.length > 0));
              const isReasoningStreaming =
                isLastTurn &&
                turn.thoughts.some((item) => liveItemIds.has(item.id));
              const isReplyStreaming =
                isLastTurn &&
                turn.replyItemIds.some((id) => liveItemIds.has(id));
              const hasReasoning = turn.thoughts.length > 0 || isThinkingNow;
              const traceNodes = renderAssistantTraceSteps(turn.steps);

              return (
                <React.Fragment key={turn.id}>
                  <MessageScrollerItem messageId={turn.id}>
                    <Message>
                      <MessageContent className="gap-3">
                        {(hasReasoning || traceNodes.length > 0) && (
                          <Trace>
                            {hasReasoning && (
                              <ReasoningGroup
                                thoughts={turn.thoughts}
                                running={isThinkingNow}
                                startedAtMs={
                                  isThinkingNow ? liveTurnStartedAtMs : null
                                }
                                streaming={isReasoningStreaming}
                              />
                            )}
                            {traceNodes}
                          </Trace>
                        )}
                        {turn.todos.length > 0 && (
                          <TodoQueue todos={turn.todos} />
                        )}
                        {turn.confirmations.map((conf) => (
                          <ToolConfirmationCard
                            key={conf.id}
                            confirmation={conf}
                          />
                        ))}
                        {turn.replyText && (
                          <SessionMessageBody
                            text={turn.replyText}
                            streaming={isReplyStreaming}
                          />
                        )}
                        {turn.sources.length > 0 && (
                          <SourcesList sources={turn.sources} />
                        )}
                        <AssistantMessageFooter
                          text={turn.replyText}
                          durationSec={turn.durationSec}
                          streaming={isTurnActive}
                          startedAtMs={
                            isTurnActive ? liveTurnStartedAtMs : null
                          }
                        />
                      </MessageContent>
                    </Message>
                  </MessageScrollerItem>
                  {turn.checkpointLabel && (
                    <CheckpointMarker label={turn.checkpointLabel} />
                  )}
                </React.Fragment>
              );
            })}
            {showPendingAssistant && (
              <MessageScrollerItem messageId="__live_pending_assistant__">
                <Message>
                  <MessageContent className="gap-3">
                    <Trace>
                      <ReasoningGroup
                        thoughts={[]}
                        running
                        startedAtMs={liveTurnStartedAtMs}
                      />
                    </Trace>
                    <AssistantMessageFooter
                      text=""
                      streaming
                      startedAtMs={liveTurnStartedAtMs}
                    />
                  </MessageContent>
                </Message>
              </MessageScrollerItem>
            )}
          </MessageScrollerContent>
        </MessageScrollerViewport>
        <MessageScrollerButton onClick={() => setScrollbarPaused(false)} />
      </MessageScroller>
    </MessageScrollerProvider>
  );
}

export function SessionChat({
  activeSessionId,
  activeSession,
  activeSessionMode,
  replayedMessages,
  hasMoreMessages = false,
  loadingMessages = false,
  loadingOlderMessages = false,
  isLiveThinking = false,
  liveTurnActive = false,
  liveTurnStartedAtMs = null,
  liveItemIds = EMPTY_LIVE_ITEM_IDS,
  onLoadOlderMessages,
  sessionModels = null,
  sessionModes = null,
  configOptions = [],
  availableCommands = [],
  sessionUsage = null,
  configBusyId = null,
  onSetConfigOption,
  onSetMode,
  includeCbiMcp,
  onToggleCbiMcp,
  sessionCaps,
  sessionBusyId,
  onCreateSession,
  onCloseSession,
  onSwitchToSessionsTab,
}: SessionChatProps) {
  const [draft, setDraft] = React.useState("");

  const turns = React.useMemo(
    () => groupMessagesIntoTurns(replayedMessages),
    [replayedMessages],
  );
  const contextUsage = React.useMemo(
    () => estimateContextUsage(replayedMessages),
    [replayedMessages],
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {activeSessionId && (
        <>
          <div className="flex shrink-0 items-center justify-between gap-2 px-3 py-2">
            <div className="flex min-w-0 items-center gap-2">
              <span
                className="truncate text-xs font-medium text-foreground"
                title={activeSession?.title || activeSessionId}
              >
                {activeSession?.title || "Untitled session"}
              </span>
            </div>
            <div className="flex shrink-0 items-center gap-1.5">
              <ContextUsageHoverCard
                usage={contextUsage}
                sessionUsage={sessionUsage}
              />
              {sessionCaps.canClose && (
                <Tooltip>
                  <TooltipTrigger
                    render={
                      <Button
                        variant="ghost"
                        size="icon-xs"
                        aria-label="Close active session"
                        disabled={sessionBusyId === "close"}
                        onClick={() => void onCloseSession()}
                      />
                    }
                  >
                    {sessionBusyId === "close" ? <Spinner /> : <XIcon />}
                  </TooltipTrigger>
                  <TooltipContent>
                    <p>Close session</p>
                  </TooltipContent>
                </Tooltip>
              )}
            </div>
          </div>
          <Separator />
        </>
      )}

      <div className="min-h-0 flex-1">
        {!activeSessionId ? (
          <Empty className="h-full">
            <EmptyHeader>
              <EmptyMedia variant="icon">
                <MessageCircleDashedIcon />
              </EmptyMedia>
              <EmptyTitle>No active conversation</EmptyTitle>
              <EmptyDescription>
                Resume an existing ACP session to browse its thread, or start a
                fresh workspace session.
              </EmptyDescription>
            </EmptyHeader>
            <EmptyContent className="flex-row justify-center gap-2">
              <Button
                size="sm"
                disabled={sessionBusyId !== null}
                onClick={() => void onCreateSession()}
              >
                {sessionBusyId === "new" ? <Spinner /> : <PlusIcon />}
                New Session
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={onSwitchToSessionsTab}
              >
                <HistoryIcon />
                Browse Sessions
              </Button>
            </EmptyContent>
          </Empty>
        ) : loadingMessages && turns.length === 0 ? (
          <Empty className="h-full">
            <EmptyHeader>
              <EmptyMedia variant="icon">
                <Spinner />
              </EmptyMedia>
              <EmptyTitle>Loading conversation…</EmptyTitle>
              <EmptyDescription>
                Fetching recent turns and syncing session history.
              </EmptyDescription>
            </EmptyHeader>
          </Empty>
        ) : turns.length === 0 && !liveTurnActive ? (
          <div className="flex h-full flex-col justify-between p-4">
            {activeSessionMode === "resumed" && (
              <CheckpointMarker label="Session resumed · prior history not replayed" />
            )}
            <Empty className="h-full">
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <MessageCircleDashedIcon />
                </EmptyMedia>
                <EmptyTitle>What should we build?</EmptyTitle>
                <EmptyDescription>
                  {activeSessionMode === "resumed"
                    ? "This session is resumed and ready. Pick a suggestion below to draft your next prompt."
                    : "The agent can search symbols, trace references, and refactor your codebase."}
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
          </div>
        ) : (
          <SessionTimelineScroller
            key={activeSessionId}
            activeSessionMode={activeSessionMode}
            turns={turns}
            hasMoreMessages={hasMoreMessages}
            loadingMessages={loadingMessages}
            loadingOlderMessages={loadingOlderMessages}
            isLiveThinking={isLiveThinking}
            liveTurnActive={liveTurnActive}
            liveTurnStartedAtMs={liveTurnStartedAtMs}
            liveItemIds={liveItemIds}
            onLoadOlderMessages={onLoadOlderMessages}
          />
        )}
      </div>

      <Separator />
      <div className="flex shrink-0 flex-col gap-2 p-3">
        {activeSessionId && !loadingMessages && turns.length === 0 && (
          <PromptSuggestions
            suggestions={DEFAULT_PROMPT_SUGGESTIONS}
            onSelect={setDraft}
          />
        )}
        <SessionPromptInput
          draft={draft}
          onDraftChange={setDraft}
          hasActiveSession={Boolean(activeSessionId)}
          promptEnabled={false}
          includeCbiMcp={includeCbiMcp}
          onToggleCbiMcp={onToggleCbiMcp}
          configOptions={configOptions}
          sessionModels={sessionModels}
          sessionModes={sessionModes}
          availableCommands={availableCommands}
          configBusyId={configBusyId}
          onSelectConfigOption={onSetConfigOption}
          onSelectMode={onSetMode}
        />
      </div>
    </div>
  );
}
