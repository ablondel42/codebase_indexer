import * as React from "react";
import {
  CircleAlertIcon,
  CircleCheckIcon,
  LoaderCircleIcon,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import {
  Trace,
  TraceCollapsible,
  TraceCollapsibleContent,
  TraceCollapsibleTrigger,
  TraceIcon,
  TraceLine,
  TraceTitle,
} from "@/components/ui/trace";
import {
  useLiveElapsedSeconds,
  useStreamingText,
} from "@/hooks/use-streaming-text";
import {
  formatDurationSec,
  type ThoughtStep,
} from "@/lib/acp/turn-grouping";

function StreamingThoughtText({
  text,
  streaming,
}: {
  text: string;
  streaming: boolean;
}) {
  const { displayedText, isStreaming } = useStreamingText(text, streaming);
  return (
    <p className="min-w-0 whitespace-pre-wrap">
      {displayedText}
      {isStreaming && (
        <span
          aria-hidden="true"
          className="inline-block w-0 overflow-visible align-baseline select-none"
        >
          <span className="inline-block h-[0.95em] w-[1.5px] translate-x-[1px] translate-y-[0.1em] animate-pulse bg-primary/70" />
        </span>
      )}
    </p>
  );
}

export function ReasoningGroup({
  thoughts,
  running = false,
  streaming = false,
}: {
  thoughts: ThoughtStep[];
  running?: boolean;
  streaming?: boolean;
}) {
  const [open, setOpen] = React.useState<boolean>();
  const totalDuration = React.useMemo(() => {
    const sum = thoughts.reduce(
      (acc, item) => acc + (item.durationSec ?? 0),
      0,
    );
    return sum > 0 ? sum : undefined;
  }, [thoughts]);
  const thoughtElapsed = useLiveElapsedSeconds(running, null, totalDuration);

  if (thoughts.length === 0) {
    return null;
  }

  const settledSeconds =
    thoughtElapsed !== undefined && thoughtElapsed > 0
      ? Math.max(1, Math.round(thoughtElapsed))
      : undefined;
  const titleText = running
    ? `Thought for ${formatDurationSec(thoughtElapsed ?? 0)}`
    : settledSeconds !== undefined
      ? `Thought for ${formatDurationSec(settledSeconds)}`
      : "Thought";

  const line = (
    <TraceLine status={running ? "running" : undefined}>
      <TraceIcon>
        {running ? (
          <LoaderCircleIcon className="animate-spin" />
        ) : (
          <CircleCheckIcon />
        )}
      </TraceIcon>
      <TraceTitle>{titleText}</TraceTitle>
    </TraceLine>
  );

  return (
    <TraceCollapsible open={open ?? running} onOpenChange={setOpen}>
      <TraceCollapsibleTrigger>{line}</TraceCollapsibleTrigger>
      <TraceCollapsibleContent className="max-h-80 overflow-y-auto overscroll-contain pr-2">
        {thoughts.length === 1 ? (
          <div className="text-xs">
            <StreamingThoughtText
              text={thoughts[0]!.text}
              streaming={streaming}
            />
          </div>
        ) : (
          <ol className="flex flex-col divide-y divide-border/40 text-xs">
            {thoughts.map((item, idx) => (
              <li
                key={item.id}
                className="flex gap-2.5 py-2 first:pt-0 last:pb-0"
              >
                <span className="shrink-0 font-mono text-[10px] leading-relaxed text-muted-foreground/60 tabular-nums select-none">
                  {idx + 1}.
                </span>
                <StreamingThoughtText
                  text={item.text}
                  streaming={streaming && idx === thoughts.length - 1}
                />
              </li>
            ))}
          </ol>
        )}
      </TraceCollapsibleContent>
    </TraceCollapsible>
  );
}

export function ReasoningRow({
  text,
  running = false,
  durationSec,
}: {
  text: string;
  running?: boolean;
  durationSec?: number;
}) {
  const [open, setOpen] = React.useState<boolean>();
  const elapsed = useLiveElapsedSeconds(running, null, durationSec);
  const roundedSec =
    elapsed !== undefined && elapsed > 0
      ? Math.max(1, Math.round(elapsed))
      : undefined;

  const line = (
    <TraceLine status={running ? "running" : undefined}>
      <TraceIcon>
        {running ? (
          <LoaderCircleIcon className="animate-spin" />
        ) : (
          <CircleCheckIcon />
        )}
      </TraceIcon>
      <TraceTitle>
        {running
          ? `Thought for ${formatDurationSec(elapsed ?? 0)}`
          : roundedSec !== undefined
            ? `Thought for ${formatDurationSec(roundedSec)}`
            : "Thought"}
      </TraceTitle>
    </TraceLine>
  );

  if (text.trim().length === 0) {
    return line;
  }

  return (
    <TraceCollapsible open={open ?? running} onOpenChange={setOpen}>
      <TraceCollapsibleTrigger>{line}</TraceCollapsibleTrigger>
      <TraceCollapsibleContent className="whitespace-pre-wrap text-xs">
        {text}
      </TraceCollapsibleContent>
    </TraceCollapsible>
  );
}

function StatusIcon({ status }: { status?: "running" | "error" }) {
  if (status === "running") {
    return <LoaderCircleIcon className="animate-spin" />;
  }
  return status === "error" ? <CircleAlertIcon /> : <CircleCheckIcon />;
}

export function ChainOfThoughtGroup({
  stepCount,
  failedCount = 0,
  running = false,
  files = [],
  children,
}: {
  stepCount: number;
  failedCount?: number;
  running?: boolean;
  files?: string[];
  children: React.ReactNode;
}) {
  const [open, setOpen] = React.useState<boolean>();
  const status: "running" | "error" | undefined = running
    ? "running"
    : failedCount > 0
      ? "error"
      : undefined;

  return (
    <TraceCollapsible open={open ?? running} onOpenChange={setOpen}>
      <TraceCollapsibleTrigger>
        <TraceLine status={status}>
          <TraceIcon>
            <StatusIcon status={status} />
          </TraceIcon>
          <TraceTitle>
            {running
              ? "Researching"
              : `Researched in ${stepCount} steps${failedCount > 0 ? `, ${failedCount} failed` : ""}`}
          </TraceTitle>
        </TraceLine>
      </TraceCollapsibleTrigger>
      <TraceCollapsibleContent keepMounted className="flex flex-col gap-2">
        {files.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {files.map((file) => (
              <Badge key={file} variant="secondary" className="font-mono">
                {file}
              </Badge>
            ))}
          </div>
        )}
        <Trace>{children}</Trace>
      </TraceCollapsibleContent>
    </TraceCollapsible>
  );
}
