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
  TraceDescription,
  TraceIcon,
  TraceLine,
  TraceTitle,
} from "@/components/ui/trace";
import type { ThoughtStep } from "@/lib/acp/turn-grouping";

export function ReasoningGroup({
  thoughts,
  running = false,
}: {
  thoughts: ThoughtStep[];
  running?: boolean;
}) {
  const [open, setOpen] = React.useState<boolean>();
  const totalDuration = React.useMemo(() => {
    const sum = thoughts.reduce(
      (acc, item) => acc + (item.durationSec ?? 0),
      0,
    );
    return sum > 0 ? sum : undefined;
  }, [thoughts]);

  const stepLabel =
    thoughts.length >= 2 ? `${thoughts.length} steps` : undefined;
  const durationLabel =
    !running && totalDuration !== undefined ? `${totalDuration}s` : undefined;
  const description = [stepLabel, durationLabel].filter(Boolean).join(" · ");

  const line = (
    <TraceLine status={running ? "running" : undefined}>
      <TraceIcon>
        {running ? (
          <LoaderCircleIcon className="animate-spin" />
        ) : (
          <CircleCheckIcon />
        )}
      </TraceIcon>
      <TraceTitle>{running ? "Thinking" : "Thought"}</TraceTitle>
      {description && <TraceDescription>· {description}</TraceDescription>}
    </TraceLine>
  );

  if (thoughts.length === 0) {
    return running ? line : null;
  }

  return (
    <TraceCollapsible open={open ?? running} onOpenChange={setOpen}>
      <TraceCollapsibleTrigger>{line}</TraceCollapsibleTrigger>
      <TraceCollapsibleContent className="max-h-80 overflow-y-auto overscroll-contain pr-2">
        {thoughts.length === 1 ? (
          <p className="whitespace-pre-wrap text-xs">{thoughts[0]!.text}</p>
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
                <p className="min-w-0 whitespace-pre-wrap">{item.text}</p>
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

  const line = (
    <TraceLine status={running ? "running" : undefined}>
      <TraceIcon>
        {running ? (
          <LoaderCircleIcon className="animate-spin" />
        ) : (
          <CircleCheckIcon />
        )}
      </TraceIcon>
      <TraceTitle>{running ? "Thinking" : "Thought"}</TraceTitle>
      {!running && durationSec !== undefined && (
        <TraceDescription>{durationSec}s</TraceDescription>
      )}
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
