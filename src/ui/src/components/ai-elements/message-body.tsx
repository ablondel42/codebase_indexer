import * as React from "react";
import {
  ArrowUpRightIcon,
  CheckIcon,
  CircleCheckIcon,
  CopyIcon,
  GlobeIcon,
  LoaderCircleIcon,
  ThumbsDownIcon,
  ThumbsUpIcon,
} from "lucide-react";

import { useHighlightedCode } from "@/hooks/use-highlighted-code";
import {
  useLiveElapsedSeconds,
  useStreamingText,
} from "@/hooks/use-streaming-text";
import { Button } from "@/components/ui/button";
import {
  CodeBlock,
  CodeBlockActions,
  CodeBlockContent,
  CodeBlockCopyButton,
  CodeBlockHeader,
  CodeBlockLine,
  CodeBlockTitle,
} from "@/components/ui/code-block";
import {
  HoverCard,
  HoverCardContent,
  HoverCardTrigger,
} from "@/components/ui/hover-card";
import { MessageFooter } from "@/components/ui/message";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
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
  extractHostname,
  formatDurationSec,
  splitMessageSegments,
} from "@/lib/acp/turn-grouping";
import type { AcpSourceItem } from "@/lib/acp/types";

export function InlineCitationPill({ source }: { source: AcpSourceItem }) {
  const host = extractHostname(source.url);

  return (
    <HoverCard>
      <HoverCardTrigger
        delay={10}
        closeDelay={100}
        render={<Button variant="secondary" size="xs" />}
      >
        {host}
      </HoverCardTrigger>
      <HoverCardContent className="flex w-64 flex-col gap-1">
        <div className="font-medium text-foreground">
          {source.title || host}
        </div>
        {source.description && (
          <p className="text-xs text-muted-foreground">{source.description}</p>
        )}
        <a
          href={source.url}
          target="_blank"
          rel="noreferrer"
          className="text-xs text-muted-foreground underline-offset-4 hover:underline"
        >
          {host}
          <span className="sr-only"> (opens in a new tab)</span>
        </a>
      </HoverCardContent>
    </HoverCard>
  );
}

function HighlightedCodeFence({
  code,
  language,
  incomplete,
}: {
  code: string;
  language: string;
  incomplete: boolean;
}) {
  const lines = useHighlightedCode(code, language || "text");

  return (
    <CodeBlock size="sm">
      <CodeBlockHeader>
        <CodeBlockTitle>{language || "text"}</CodeBlockTitle>
        <CodeBlockActions>
          <CodeBlockCopyButton value={code} disabled={incomplete} />
        </CodeBlockActions>
      </CodeBlockHeader>
      <CodeBlockContent>
        {lines.map((line, index) => (
          <CodeBlockLine key={index}>{line}</CodeBlockLine>
        ))}
      </CodeBlockContent>
    </CodeBlock>
  );
}

const INLINE_TOKEN_RE = /\[([^\]\n]+)\]\(([^)\s\n]+)\)|`([^`\n]+)`|\*\*([^*\n]+)\*\*/g;
const TRAILING_INCOMPLETE_LINK_RE = /\[([^\]\n]+)\]\(([^)\s\n]*)$/;
const TRAILING_INCOMPLETE_CODE_RE = /`([^`\n]*)$/;
const TRAILING_INCOMPLETE_BOLD_RE = /\*\*([^*\n]*)$/;

function renderLinkLabel(label: string): {
  isCode: boolean;
  text: string;
} {
  if (label.startsWith("`") && label.endsWith("`") && label.length >= 2) {
    return { isCode: true, text: label.slice(1, -1) };
  }
  return { isCode: false, text: label };
}

function renderMarkdownLinkNode(
  key: string | number,
  label: string,
  url: string,
): React.ReactNode {
  const parsed = renderLinkLabel(label);
  const isHttp = url.startsWith("http://") || url.startsWith("https://");
  if (isHttp) {
    return (
      <a
        key={key}
        href={url}
        target="_blank"
        rel="noreferrer"
        className="font-medium underline underline-offset-4 hover:text-primary"
      >
        {parsed.isCode ? (
          <code className="rounded-sm bg-muted px-1 py-0.5 font-mono text-[0.9em]">
            {parsed.text}
          </code>
        ) : (
          parsed.text
        )}
      </a>
    );
  }
  return (
    <code
      key={key}
      title={url}
      className="rounded-sm bg-muted px-1 py-0.5 font-mono text-[0.9em]"
    >
      {parsed.text}
    </code>
  );
}

function renderClosedInlineTokens(
  text: string,
  keyPrefix: string,
): { nodes: React.ReactNode[]; tail: string } {
  const nodes: React.ReactNode[] = [];
  let lastIndex = 0;
  let tokenIdx = 0;
  INLINE_TOKEN_RE.lastIndex = 0;

  for (const match of text.matchAll(INLINE_TOKEN_RE)) {
    const start = match.index ?? 0;
    if (start > lastIndex) {
      nodes.push(
        <React.Fragment key={`${keyPrefix}-t-${tokenIdx}`}>
          {text.slice(lastIndex, start)}
        </React.Fragment>,
      );
      tokenIdx += 1;
    }

    const linkLabel = match[1];
    const linkUrl = match[2];
    const codeText = match[3];
    const boldText = match[4];

    if (linkLabel !== undefined && linkUrl !== undefined) {
      nodes.push(
        renderMarkdownLinkNode(`${keyPrefix}-l-${tokenIdx}`, linkLabel, linkUrl),
      );
    } else if (codeText !== undefined) {
      nodes.push(
        <code
          key={`${keyPrefix}-c-${tokenIdx}`}
          className="rounded-sm bg-muted px-1 py-0.5 font-mono text-[0.9em]"
        >
          {codeText}
        </code>,
      );
    } else if (boldText !== undefined) {
      nodes.push(
        <strong key={`${keyPrefix}-b-${tokenIdx}`} className="font-semibold">
          {boldText}
        </strong>,
      );
    }

    lastIndex = start + match[0].length;
    tokenIdx += 1;
  }

  return {
    nodes,
    tail: text.slice(lastIndex),
  };
}

function renderInlineFormattedText(
  paragraph: string,
  streaming: boolean,
): React.ReactNode[] {
  if (!streaming) {
    const { nodes, tail } = renderClosedInlineTokens(paragraph, "s");
    if (tail.length > 0) {
      nodes.push(<React.Fragment key="s-tail">{tail}</React.Fragment>);
    }
    return nodes;
  }

  // Handle an in-flight trailing markdown link first so `[file.py](file:///...`
  // renders cleanly as `file.py` immediately without flashing raw URL paths or
  // splitting an inner backtick before the closing parenthesis arrives.
  const trailingLink = TRAILING_INCOMPLETE_LINK_RE.exec(paragraph);
  if (trailingLink) {
    const prefix = paragraph.slice(0, trailingLink.index);
    const backtickCount = (prefix.match(/`/g) ?? []).length;
    if (backtickCount % 2 === 0) {
      const linkLabel = trailingLink[1] ?? "";
      const partialUrl = trailingLink[2] ?? "";
      const parsedLabel = renderLinkLabel(linkLabel);
      const isPartialHttp =
        partialUrl.startsWith("http://") || partialUrl.startsWith("https://");
      const nodes = renderInlineFormattedText(prefix, true);
      if (isPartialHttp && !parsedLabel.isCode) {
        nodes.push(
          <span
            key="trailing-link"
            className="font-medium underline underline-offset-4"
          >
            {parsedLabel.text}
          </span>,
        );
      } else {
        nodes.push(
          <code
            key="trailing-link"
            className="rounded-sm bg-muted px-1 py-0.5 font-mono text-[0.9em]"
          >
            {parsedLabel.text}
          </code>,
        );
      }
      return nodes;
    }
  }

  const { nodes, tail } = renderClosedInlineTokens(paragraph, "d");
  if (tail.length === 0) {
    return nodes;
  }

  const trailingCode = TRAILING_INCOMPLETE_CODE_RE.exec(tail);
  if (trailingCode) {
    const before = tail.slice(0, trailingCode.index);
    const partialCode = trailingCode[1] ?? "";
    if (before.length > 0) {
      nodes.push(<React.Fragment key="d-tc-pre">{before}</React.Fragment>);
    }
    if (partialCode.length > 0) {
      nodes.push(
        <code
          key="d-tc"
          className="rounded-sm bg-muted px-1 py-0.5 font-mono text-[0.9em]"
        >
          {partialCode}
        </code>,
      );
    }
    return nodes;
  }

  const trailingBold = TRAILING_INCOMPLETE_BOLD_RE.exec(tail);
  if (trailingBold) {
    const before = tail.slice(0, trailingBold.index);
    const partialBold = trailingBold[1] ?? "";
    if (before.length > 0) {
      nodes.push(<React.Fragment key="d-tb-pre">{before}</React.Fragment>);
    }
    if (partialBold.length > 0) {
      nodes.push(
        <strong key="d-tb" className="font-semibold">
          {partialBold}
        </strong>,
      );
    }
    return nodes;
  }

  nodes.push(<React.Fragment key="d-tail">{tail}</React.Fragment>);
  return nodes;
}

function StreamingCaret({ className }: { className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={
        className ??
        "inline-block w-0 overflow-visible align-baseline select-none"
      }
    >
      <span className="inline-block h-[1em] w-[1.5px] translate-x-[1px] translate-y-[0.12em] animate-pulse bg-primary/80" />
    </span>
  );
}

export function SessionMessageBody({
  text,
  streaming = false,
}: {
  text: string;
  streaming?: boolean;
}) {
  const { displayedText, isStreaming } = useStreamingText(text, streaming);
  const segments = React.useMemo(
    () => splitMessageSegments(displayedText),
    [displayedText],
  );

  if (segments.length === 0) {
    if (!streaming) {
      return null;
    }
    return (
      <div className="flex min-w-0 items-center text-sm leading-relaxed">
        <span
          aria-hidden="true"
          className="inline-block h-3.5 w-[1.5px] animate-pulse bg-primary/80 select-none"
        />
      </div>
    );
  }

  return (
    <div className="flex min-w-0 flex-col gap-3 text-sm leading-relaxed">
      {segments.map((segment, idx) => {
        const isLastSegment = idx === segments.length - 1;
        if (segment.kind === "code") {
          return (
            <React.Fragment key={idx}>
              <HighlightedCodeFence
                code={segment.code}
                language={segment.language}
                incomplete={segment.incomplete}
              />
              {isStreaming && isLastSegment && (
                <span
                  aria-hidden="true"
                  className="inline-block h-3.5 w-[1.5px] animate-pulse bg-primary/80 select-none"
                />
              )}
            </React.Fragment>
          );
        }

        const paragraphs = segment.text.split(/\n{2,}/);
        return (
          <div key={idx} className="flex flex-col gap-2">
            {paragraphs.map((para, pIdx) => {
              const showCursor =
                isStreaming &&
                isLastSegment &&
                pIdx === paragraphs.length - 1;
              return (
                <p
                  key={pIdx}
                  className="whitespace-pre-wrap wrap-break-word text-foreground"
                >
                  {renderInlineFormattedText(para, showCursor)}
                  {showCursor && <StreamingCaret />}
                </p>
              );
            })}
            {segment.citations.length > 0 && (
              <div className="flex flex-wrap items-center gap-1.5">
                {segment.citations.map((citation) => (
                  <InlineCitationPill key={citation.url} source={citation} />
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

export function SourcesList({ sources }: { sources: AcpSourceItem[] }) {
  if (sources.length === 0) {
    return null;
  }

  return (
    <Trace>
      <TraceCollapsible>
        <TraceCollapsibleTrigger>
          <TraceLine>
            <TraceIcon>
              <GlobeIcon />
            </TraceIcon>
            <TraceTitle>
              Used {sources.length}{" "}
              {sources.length === 1 ? "source" : "sources"}
            </TraceTitle>
          </TraceLine>
        </TraceCollapsibleTrigger>
        <TraceCollapsibleContent>
          <Trace>
            {sources.map((source) => {
              const host = extractHostname(source.url);
              return (
                <a
                  key={source.url}
                  href={source.url}
                  target="_blank"
                  rel="noreferrer"
                >
                  <TraceLine>
                    <TraceIcon>
                      <GlobeIcon />
                    </TraceIcon>
                    <TraceTitle>
                      {source.title || host}
                      <span className="sr-only"> (opens in a new tab)</span>
                    </TraceTitle>
                    <TraceIcon>
                      <ArrowUpRightIcon />
                    </TraceIcon>
                  </TraceLine>
                </a>
              );
            })}
          </Trace>
        </TraceCollapsibleContent>
      </TraceCollapsible>
    </Trace>
  );
}

export function MessageAction({
  label,
  children,
  ...props
}: React.ComponentProps<typeof Button> & { label: string }) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label={label}
            {...props}
          />
        }
      >
        {children}
      </TooltipTrigger>
      <TooltipContent>
        <p>{label}</p>
      </TooltipContent>
    </Tooltip>
  );
}

export function UserMessageFooter({ text }: { text: string }) {
  const [copied, setCopied] = React.useState(false);

  React.useEffect(() => {
    if (!copied) {
      return;
    }
    const timer = window.setTimeout(() => setCopied(false), 2000);
    return () => window.clearTimeout(timer);
  }, [copied]);

  return (
    <MessageFooter className="opacity-0 transition-opacity group-focus-within/message:opacity-100 group-hover/message:opacity-100">
      <MessageAction
        label={copied ? "Copied" : "Copy"}
        onClick={() => {
          navigator.clipboard
            .writeText(text)
            .then(() => setCopied(true))
            .catch(() => {});
        }}
      >
        {copied ? <CheckIcon /> : <CopyIcon />}
      </MessageAction>
    </MessageFooter>
  );
}

export function AssistantMessageFooter({
  text,
  durationSec,
  streaming = false,
  isThinking = false,
  startedAtMs = null,
}: {
  text: string;
  durationSec?: number;
  streaming?: boolean;
  isThinking?: boolean;
  startedAtMs?: number | null;
}) {
  const [copied, setCopied] = React.useState(false);
  const [feedback, setFeedback] = React.useState<"like" | "dislike" | null>(
    null,
  );
  const elapsed = useLiveElapsedSeconds(streaming, startedAtMs, durationSec);
  const showStatus = streaming || (elapsed !== undefined && elapsed > 0);

  React.useEffect(() => {
    if (!copied) {
      return;
    }
    const timer = window.setTimeout(() => setCopied(false), 2000);
    return () => window.clearTimeout(timer);
  }, [copied]);

  if (!text && !showStatus) {
    return null;
  }

  return (
    <MessageFooter className="w-full items-center px-0">
      {text && (
        <>
          <MessageAction
            label={copied ? "Copied" : "Copy"}
            onClick={() => {
              navigator.clipboard
                .writeText(text)
                .then(() => setCopied(true))
                .catch(() => {});
            }}
          >
            {copied ? <CheckIcon /> : <CopyIcon />}
          </MessageAction>
          <MessageAction
            label="Good response"
            aria-pressed={feedback === "like"}
            onClick={() => setFeedback(feedback === "like" ? null : "like")}
          >
            <ThumbsUpIcon
              className={feedback === "like" ? "fill-current" : undefined}
            />
          </MessageAction>
          <MessageAction
            label="Bad response"
            aria-pressed={feedback === "dislike"}
            onClick={() =>
              setFeedback(feedback === "dislike" ? null : "dislike")
            }
          >
            <ThumbsDownIcon
              className={feedback === "dislike" ? "fill-current" : undefined}
            />
          </MessageAction>
        </>
      )}
      {showStatus && (
        <span className="ml-auto inline-flex items-center gap-1.5 text-xs text-muted-foreground tabular-nums">
          {streaming ? (
            <>
              <LoaderCircleIcon className="size-3.5 animate-spin" />
              <span>
                {isThinking ? "Thinking" : "Working"}{" "}
                {formatDurationSec(elapsed ?? 0)}
              </span>
            </>
          ) : (
            <>
              <CircleCheckIcon className="size-3.5" />
              <span>
                Worked for{" "}
                {formatDurationSec(Math.max(1, Math.round(elapsed ?? 1)))}
              </span>
            </>
          )}
        </span>
      )}
    </MessageFooter>
  );
}
