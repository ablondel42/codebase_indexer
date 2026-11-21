import * as React from "react";
import {
  ArrowUpRightIcon,
  CheckIcon,
  CopyIcon,
  GlobeIcon,
  ThumbsDownIcon,
  ThumbsUpIcon,
} from "lucide-react";

import { useHighlightedCode } from "@/hooks/use-highlighted-code";
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

function renderInlineCodeText(paragraph: string): React.ReactNode[] {
  const parts = paragraph.split(/(`[^`]+`)/g);
  return parts.map((part, idx) => {
    if (part.startsWith("`") && part.endsWith("`") && part.length >= 2) {
      return (
        <code
          key={idx}
          className="rounded-sm bg-muted px-1 py-0.5 font-mono text-[0.9em]"
        >
          {part.slice(1, -1)}
        </code>
      );
    }
    return <React.Fragment key={idx}>{part}</React.Fragment>;
  });
}

export function SessionMessageBody({ text }: { text: string }) {
  const segments = React.useMemo(() => splitMessageSegments(text), [text]);

  if (segments.length === 0) {
    return null;
  }

  return (
    <div className="flex min-w-0 flex-col gap-3 text-sm leading-relaxed">
      {segments.map((segment, idx) => {
        if (segment.kind === "code") {
          return (
            <HighlightedCodeFence
              key={idx}
              code={segment.code}
              language={segment.language}
              incomplete={segment.incomplete}
            />
          );
        }

        const paragraphs = segment.text.split(/\n{2,}/);
        return (
          <div key={idx} className="flex flex-col gap-2">
            {paragraphs.map((para, pIdx) => (
              <p
                key={pIdx}
                className="whitespace-pre-wrap wrap-break-word text-foreground"
              >
                {renderInlineCodeText(para)}
              </p>
            ))}
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

export function AssistantMessageFooter({ text }: { text: string }) {
  const [copied, setCopied] = React.useState(false);
  const [feedback, setFeedback] = React.useState<"like" | "dislike" | null>(
    null,
  );

  React.useEffect(() => {
    if (!copied) {
      return;
    }
    const timer = window.setTimeout(() => setCopied(false), 2000);
    return () => window.clearTimeout(timer);
  }, [copied]);

  if (!text) {
    return null;
  }

  return (
    <MessageFooter className="px-0">
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
        onClick={() => setFeedback(feedback === "dislike" ? null : "dislike")}
      >
        <ThumbsDownIcon
          className={feedback === "dislike" ? "fill-current" : undefined}
        />
      </MessageAction>
    </MessageFooter>
  );
}
