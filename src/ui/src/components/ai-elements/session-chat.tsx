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
  ReasoningRow,
} from "@/components/ai-elements/reasoning-trace";
import { ToolRow } from "@/components/ai-elements/tool-trace";
import {
  CheckpointMarker,
  ContextUsageHoverCard,
  SessionArtifactsSheet,
  ToolConfirmationCard,
} from "@/components/ai-elements/turn-prompts-and-files";
import { Badge } from "@/components/ui/badge";
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
  AcpReplayedMessage,
  AcpSessionArtifactContent,
  AcpSessionArtifactSummary,
  AcpSessionCapabilitiesFlags,
  AcpSessionInfo,
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
    } else if (step.role === "thought") {
      nodes.push(
        <ReasoningRow
          key={step.id}
          text={step.text}
          running={false}
          durationSec={step.durationSec}
        />,
      );
    } else if (step.role === "tool") {
      nodes.push(<ToolRow key={step.id} step={step} />);
    }

    idx += 1;
  }

  return nodes;
}

export interface SessionChatProps {
  activeSessionId: string | null;
  activeSession: AcpSessionInfo | undefined;
  activeSessionMode: AcpActiveSessionMode;
  replayedMessages: AcpReplayedMessage[];
  sessionArtifacts: AcpSessionArtifactSummary[];
  onLoadArtifact: (name: string) => Promise<AcpSessionArtifactContent | null>;
  includeCbiMcp: boolean;
  onToggleCbiMcp: (enabled: boolean) => void;
  sessionCaps: AcpSessionCapabilitiesFlags;
  sessionBusyId: string | null;
  onCreateSession: () => Promise<void>;
  onCloseSession: () => Promise<void>;
  onSwitchToSessionsTab: () => void;
}

export function SessionChat({
  activeSessionId,
  activeSession,
  activeSessionMode,
  replayedMessages,
  sessionArtifacts,
  onLoadArtifact,
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
    <MessageScrollerProvider autoScroll>
      <div className="flex min-h-0 flex-1 flex-col">
        {activeSessionId && (
          <>
            <div className="flex shrink-0 items-center justify-between gap-2 px-3 py-2">
              <div className="flex min-w-0 items-center gap-2">
                <Badge variant="secondary">Active</Badge>
                <span
                  className="truncate text-xs font-medium text-foreground"
                  title={activeSession?.title || activeSessionId}
                >
                  {activeSession?.title || "Untitled session"}
                </span>
              </div>
              <div className="flex shrink-0 items-center gap-1.5">
                <ContextUsageHoverCard usage={contextUsage} />
                <SessionArtifactsSheet
                  artifacts={sessionArtifacts}
                  onLoadArtifact={onLoadArtifact}
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
                  Load an existing ACP session to replay its thread, or start a
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
          ) : turns.length === 0 ? (
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
            <MessageScroller>
              <MessageScrollerViewport>
                <MessageScrollerContent className="gap-5 px-3 py-4">
                  {activeSessionMode === "resumed" && (
                    <CheckpointMarker label="Session resumed · prior history not replayed" />
                  )}
                  {turns.map((turn) => {
                    if (turn.kind === "user") {
                      return (
                        <React.Fragment key={turn.id}>
                          <MessageScrollerItem
                            messageId={turn.id}
                            scrollAnchor
                          >
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

                    const traceNodes = renderAssistantTraceSteps(turn.steps);

                    return (
                      <React.Fragment key={turn.id}>
                        <MessageScrollerItem messageId={turn.id}>
                          <Message>
                            <MessageContent className="gap-3">
                              {traceNodes.length > 0 && (
                                <Trace>{traceNodes}</Trace>
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
                                <SessionMessageBody text={turn.replyText} />
                              )}
                              {turn.sources.length > 0 && (
                                <SourcesList sources={turn.sources} />
                              )}
                              <AssistantMessageFooter text={turn.replyText} />
                            </MessageContent>
                          </Message>
                        </MessageScrollerItem>
                        {turn.checkpointLabel && (
                          <CheckpointMarker label={turn.checkpointLabel} />
                        )}
                      </React.Fragment>
                    );
                  })}
                </MessageScrollerContent>
              </MessageScrollerViewport>
              <MessageScrollerButton />
            </MessageScroller>
          )}
        </div>

        <Separator />
        <div className="flex shrink-0 flex-col gap-2 p-3">
          {activeSessionId && turns.length === 0 && (
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
          />
        </div>
      </div>
    </MessageScrollerProvider>
  );
}
