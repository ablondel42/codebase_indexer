import * as React from "react";
import {
  CheckIcon,
  ChevronRightIcon,
  CircleDashedIcon,
  CircleXIcon,
  FileCodeIcon,
  FileTextIcon,
  FlaskConicalIcon,
  LayersIcon,
  TerminalIcon,
  XIcon,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { CodeBlock, CodeBlockContent } from "@/components/ui/code-block";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
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
import {
  formatDurationSec,
  parseStackTrace,
  type ParsedStackTrace,
} from "@/lib/acp/turn-grouping";
import type {
  AcpReplayedMessage,
  AcpTestCaseResult,
  AcpTestSuiteResult,
} from "@/lib/acp/types";

export function StackTraceRow({ parsed }: { parsed: ParsedStackTrace }) {
  return (
    <TraceCollapsible defaultOpen>
      <TraceCollapsibleTrigger>
        <TraceLine status="error">
          <TraceIcon>
            <CircleXIcon />
          </TraceIcon>
          <TraceTitle>Failed: {parsed.error}</TraceTitle>
        </TraceLine>
      </TraceCollapsibleTrigger>
      <TraceCollapsibleContent keepMounted className="flex flex-col gap-2">
        <p className="font-mono text-xs text-destructive">{parsed.error}</p>
        <Trace>
          {parsed.appFrames.map((frame) => (
            <TraceLine key={frame.line}>
              <TraceIcon>
                <FileCodeIcon />
              </TraceIcon>
              <TraceTitle>
                {frame.name
                  ? `${frame.name} in ${frame.file}:${frame.lineNumber}`
                  : frame.line}
              </TraceTitle>
            </TraceLine>
          ))}
          {parsed.internalFrames.length > 0 && (
            <TraceCollapsible>
              <TraceCollapsibleTrigger>
                <TraceLine>
                  <TraceIcon>
                    <LayersIcon />
                  </TraceIcon>
                  <TraceTitle>
                    {parsed.internalFrames.length} framework{" "}
                    {parsed.internalFrames.length === 1 ? "frame" : "frames"}
                  </TraceTitle>
                </TraceLine>
              </TraceCollapsibleTrigger>
              <TraceCollapsibleContent>
                <ol className="flex flex-col gap-1 overflow-x-auto rounded-md bg-muted p-2.5 font-mono text-xs">
                  {parsed.internalFrames.map((frame) => (
                    <li key={frame.line} className="whitespace-nowrap">
                      {frame.line}
                    </li>
                  ))}
                </ol>
              </TraceCollapsibleContent>
            </TraceCollapsible>
          )}
        </Trace>
      </TraceCollapsibleContent>
    </TraceCollapsible>
  );
}

function countTests(tests: AcpTestCaseResult[]) {
  return {
    total: tests.length,
    failed: tests.filter((t) => t.status === "failed").length,
    skipped: tests.filter((t) => t.status === "skipped").length,
  };
}

function formatTestCounts(counts: ReturnType<typeof countTests>): string {
  return [
    `${counts.total} ${counts.total === 1 ? "test" : "tests"}`,
    counts.failed > 0 && `${counts.failed} failed`,
    counts.skipped > 0 && `${counts.skipped} skipped`,
  ]
    .filter(Boolean)
    .join(", ");
}

function SingleTestRow({ test }: { test: AcpTestCaseResult }) {
  if (test.status === "failed" && test.error) {
    return (
      <TraceCollapsible defaultOpen>
        <TraceCollapsibleTrigger>
          <TraceLine status="error">
            <TraceIcon>
              <XIcon />
            </TraceIcon>
            <TraceTitle>{test.name}</TraceTitle>
          </TraceLine>
        </TraceCollapsibleTrigger>
        <TraceCollapsibleContent>
          <CodeBlock size="sm" variant="destructive">
            <CodeBlockContent wrap className="max-h-64">
              {test.error}
            </CodeBlockContent>
          </CodeBlock>
        </TraceCollapsibleContent>
      </TraceCollapsible>
    );
  }

  return (
    <TraceLine status={test.status === "failed" ? "error" : undefined}>
      <TraceIcon>
        {test.status === "skipped" ? (
          <CircleDashedIcon />
        ) : test.status === "failed" ? (
          <XIcon />
        ) : (
          <CheckIcon />
        )}
      </TraceIcon>
      <TraceTitle>
        {test.status === "skipped" ? `Skipped ${test.name}` : test.name}
      </TraceTitle>
    </TraceLine>
  );
}

export function TestResultsRow({
  command,
  suites,
  running = false,
}: {
  command?: string;
  suites: AcpTestSuiteResult[];
  running?: boolean;
}) {
  if (running && suites.length === 0) {
    return (
      <TraceLine status="running">
        <TraceIcon>
          <FlaskConicalIcon />
        </TraceIcon>
        <TraceTitle>Running {command || "tests"}</TraceTitle>
      </TraceLine>
    );
  }

  const summary = countTests(suites.flatMap((suite) => suite.tests));

  return (
    <TraceCollapsible defaultOpen={summary.failed > 0}>
      <TraceCollapsibleTrigger>
        <TraceLine status={summary.failed > 0 ? "error" : undefined}>
          <TraceIcon>
            <FlaskConicalIcon />
          </TraceIcon>
          <TraceTitle>Ran {formatTestCounts(summary)}</TraceTitle>
        </TraceLine>
      </TraceCollapsibleTrigger>
      <TraceCollapsibleContent keepMounted>
        <Trace>
          {suites.map((suite) => {
            const counts = countTests(suite.tests);
            return (
              <TraceCollapsible
                key={suite.name}
                defaultOpen={counts.failed > 0}
              >
                <TraceCollapsibleTrigger>
                  <TraceLine status={counts.failed > 0 ? "error" : undefined}>
                    <TraceIcon>
                      <FileCodeIcon />
                    </TraceIcon>
                    <TraceTitle>
                      {suite.name}, {formatTestCounts(counts)}
                    </TraceTitle>
                  </TraceLine>
                </TraceCollapsibleTrigger>
                <TraceCollapsibleContent keepMounted>
                  <Trace>
                    {suite.tests.map((test) => (
                      <SingleTestRow key={test.name} test={test} />
                    ))}
                  </Trace>
                </TraceCollapsibleContent>
              </TraceCollapsible>
            );
          })}
        </Trace>
      </TraceCollapsibleContent>
    </TraceCollapsible>
  );
}

export function TerminalRow({
  command,
  output,
  running = false,
  exitCode,
  failed = false,
  durationSec,
}: {
  command: string;
  output: string;
  running?: boolean;
  exitCode?: number | null;
  failed?: boolean;
  durationSec?: number;
}) {
  const [open, setOpen] = React.useState<boolean>();
  const isError =
    !running && (failed || (exitCode !== undefined && exitCode !== null && exitCode !== 0));
  const title = running
    ? `Running ${command}`
    : isError && exitCode !== undefined && exitCode !== null
      ? `${command} exited with code ${exitCode}`
      : isError
        ? `Failed to run ${command}`
        : `Ran ${command}`;
  const durationBadge =
    !running && durationSec !== undefined && durationSec >= 0.05 ? (
      <TraceDescription>· {formatDurationSec(durationSec)}</TraceDescription>
    ) : null;

  if (!output.trim()) {
    return (
      <TraceLine status={running ? "running" : isError ? "error" : undefined}>
        <TraceIcon>
          <TerminalIcon />
        </TraceIcon>
        <TraceTitle>{title}</TraceTitle>
        {durationBadge}
      </TraceLine>
    );
  }

  return (
    <TraceCollapsible
      open={open ?? (running && output.length > 0)}
      onOpenChange={setOpen}
    >
      <TraceCollapsibleTrigger>
        <TraceLine status={running ? "running" : isError ? "error" : undefined}>
          <TraceIcon>
            <TerminalIcon />
          </TraceIcon>
          <TraceTitle>{title}</TraceTitle>
          {durationBadge}
        </TraceLine>
      </TraceCollapsibleTrigger>
      <TraceCollapsibleContent>
        <CodeBlock size="sm" variant={isError ? "destructive" : "default"}>
          <CodeBlockContent wrap className="max-h-64">
            {output}
          </CodeBlockContent>
        </CodeBlock>
      </TraceCollapsibleContent>
    </TraceCollapsible>
  );
}

export function ToolRow({ step }: { step: AcpReplayedMessage }) {
  const label = step.toolTitle || step.toolKind || "tool call";
  const running = step.toolStatus === "running";
  const denied = step.toolStatus === "denied";
  const failed = step.toolStatus === "error";

  if (step.testSuites && step.testSuites.length > 0) {
    return (
      <TestResultsRow
        command={label}
        suites={step.testSuites}
        running={running}
      />
    );
  }

  if (denied) {
    return (
      <TraceLine>
        <TraceIcon>
          <TerminalIcon />
        </TraceIcon>
        <TraceTitle>Denied {label}</TraceTitle>
      </TraceLine>
    );
  }

  if (failed && step.text) {
    const parsedStack = parseStackTrace(step.text);
    if (parsedStack) {
      return <StackTraceRow parsed={parsedStack} />;
    }
  }

  if (step.toolKind === "execute" || step.exitCode !== undefined) {
    return (
      <TerminalRow
        command={label}
        output={step.text}
        running={running}
        exitCode={step.exitCode}
        failed={failed}
        durationSec={step.durationSec}
      />
    );
  }

  const durationBadge =
    !running && step.durationSec !== undefined && step.durationSec >= 0.05 ? (
      <TraceDescription>· {formatDurationSec(step.durationSec)}</TraceDescription>
    ) : null;

  const titlePrefix = running
    ? "Running"
    : failed
      ? "Failed"
      : step.toolStatus === "pending"
        ? "Pending"
        : "Ran";
  const formattedTitle = label.match(/^(Ran|Running|Read|Reading|Searched|Searching)\b/i)
    ? label
    : `${titlePrefix} ${label}`;

  const outputLines = step.text ? step.text.split("\n") : [];

  // Long file or tool outputs open cleanly in a Sheet (tool.md#output-in-a-sheet)
  if (!running && !failed && outputLines.length > 30) {
    return (
      <Sheet>
        <SheetTrigger>
          <TraceLine>
            <TraceIcon>
              <FileTextIcon />
            </TraceIcon>
            <TraceTitle>{formattedTitle}</TraceTitle>
            {durationBadge}
            <TraceIcon>
              <ChevronRightIcon />
            </TraceIcon>
          </TraceLine>
        </SheetTrigger>
        <SheetContent className="sm:max-w-xl">
          <SheetHeader>
            <SheetTitle className="font-mono text-sm">
              {step.locations?.[0] || label}
            </SheetTitle>
            <SheetDescription>{outputLines.length} lines</SheetDescription>
          </SheetHeader>
          <div className="min-h-0 flex-1 overflow-auto px-4 pb-4">
            <CodeBlock size="sm">
              <CodeBlockContent lineNumbers>{step.text}</CodeBlockContent>
            </CodeBlock>
          </div>
        </SheetContent>
      </Sheet>
    );
  }

  if (!step.text.trim() && (!step.locations || step.locations.length === 0)) {
    return (
      <TraceLine
        status={
          running
            ? "running"
            : failed
              ? "error"
              : step.toolStatus === "pending"
                ? "pending"
                : undefined
        }
      >
        <TraceIcon>
          <TerminalIcon />
        </TraceIcon>
        <TraceTitle>{formattedTitle}</TraceTitle>
        {durationBadge}
      </TraceLine>
    );
  }

  return (
    <TraceCollapsible defaultOpen={failed}>
      <TraceCollapsibleTrigger>
        <TraceLine status={running ? "running" : failed ? "error" : undefined}>
          <TraceIcon>
            <TerminalIcon />
          </TraceIcon>
          <TraceTitle>{formattedTitle}</TraceTitle>
          {durationBadge}
        </TraceLine>
      </TraceCollapsibleTrigger>
      <TraceCollapsibleContent className="flex flex-col gap-2">
        {step.locations && step.locations.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {step.locations.map((file) => (
              <Badge key={file} variant="secondary" className="font-mono">
                {file}
              </Badge>
            ))}
          </div>
        )}
        {step.text.trim().length > 0 && (
          <CodeBlock size="sm" variant={failed ? "destructive" : "default"}>
            <CodeBlockContent wrap className="max-h-64">
              {step.text}
            </CodeBlockContent>
          </CodeBlock>
        )}
      </TraceCollapsibleContent>
    </TraceCollapsible>
  );
}
