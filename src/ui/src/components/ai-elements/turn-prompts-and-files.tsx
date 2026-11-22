import * as React from "react";
import {
  ChevronRightIcon,
  FileIcon,
  FileTextIcon,
  FolderIcon,
  TerminalIcon,
} from "lucide-react";

import { SessionMessageBody } from "@/components/ai-elements/message-body";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  HoverCard,
  HoverCardContent,
  HoverCardTrigger,
} from "@/components/ui/hover-card";
import {
  Item,
  ItemActions,
  ItemContent,
  ItemDescription,
  ItemMedia,
  ItemTitle,
} from "@/components/ui/item";
import { Marker, MarkerContent } from "@/components/ui/marker";
import { Progress, ProgressLabel } from "@/components/ui/progress";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { Spinner } from "@/components/ui/spinner";
import {
  buildFileTree,
  formatCompactTokens,
  type FileTreeNode,
} from "@/lib/acp/turn-grouping";
import type {
  AcpContextUsage,
  AcpPendingConfirmation,
  AcpSessionArtifactContent,
  AcpSessionArtifactSummary,
  AcpSessionUsage,
} from "@/lib/acp/types";

export function ToolConfirmationCard({
  confirmation,
  onRespond,
}: {
  confirmation: AcpPendingConfirmation;
  onRespond?: (id: string, approved: boolean) => void;
}) {
  return (
    <Item variant="outline" size="sm">
      <ItemMedia variant="icon">
        <TerminalIcon />
      </ItemMedia>
      <ItemContent>
        <ItemTitle>Run {confirmation.command}?</ItemTitle>
        <ItemDescription>
          {confirmation.description || "The command runs in this workspace."}
        </ItemDescription>
      </ItemContent>
      <ItemActions>
        <Button
          size="sm"
          variant="outline"
          disabled={!onRespond}
          onClick={() => onRespond?.(confirmation.id, false)}
        >
          Deny
        </Button>
        <Button
          size="sm"
          disabled={!onRespond}
          onClick={() => onRespond?.(confirmation.id, true)}
        >
          Approve
        </Button>
      </ItemActions>
    </Item>
  );
}

export function CheckpointMarker({
  label,
  onRestore,
}: {
  label: string;
  onRestore?: () => void;
}) {
  return (
    <Marker variant="separator">
      <MarkerContent className="inline-flex items-center gap-2 text-xs">
        {label}
        {onRestore && (
          <Button
            size="xs"
            variant="outline"
            aria-label={`Restore to ${label}`}
            onClick={onRestore}
          >
            Restore
          </Button>
        )}
      </MarkerContent>
    </Marker>
  );
}

function FileTreeItem({
  node,
  selected,
  onSelect,
}: {
  node: FileTreeNode;
  selected?: string;
  onSelect?: (path: string) => void;
}) {
  if (node.type === "folder") {
    return (
      <Collapsible defaultOpen>
        <CollapsibleTrigger
          render={
            <Button
              variant="ghost"
              size="sm"
              className="w-full justify-start font-normal aria-expanded:bg-transparent aria-expanded:hover:bg-muted dark:aria-expanded:hover:bg-muted/50"
            />
          }
        >
          <ChevronRightIcon className="transition-transform group-data-panel-open/button:rotate-90" />
          <FolderIcon />
          <span className="truncate">{node.name}</span>
        </CollapsibleTrigger>
        <CollapsibleContent>
          <div className="ml-4 flex flex-col">
            {node.children.map((child) => (
              <FileTreeItem
                key={child.path}
                node={child}
                selected={selected}
                onSelect={onSelect}
              />
            ))}
          </div>
        </CollapsibleContent>
      </Collapsible>
    );
  }

  const isSelected = selected === node.path;
  return (
    <Button
      variant="ghost"
      size="sm"
      className={`w-full justify-start font-normal ${
        isSelected ? "bg-accent text-accent-foreground" : ""
      }`}
      aria-current={isSelected ? "true" : undefined}
      onClick={() => onSelect?.(node.path)}
    >
      <FileIcon />
      <span className="truncate">{node.name}</span>
    </Button>
  );
}

export function FileTree({
  paths,
  selected,
  onSelect,
}: {
  paths: string[];
  selected?: string;
  onSelect?: (path: string) => void;
}) {
  const nodes = React.useMemo(() => buildFileTree(paths), [paths]);

  if (nodes.length === 0) {
    return null;
  }

  return (
    <div className="flex w-full flex-col rounded-lg border border-border bg-card/40 p-1.5 text-xs">
      {nodes.map((node) => (
        <FileTreeItem
          key={node.path}
          node={node}
          selected={selected}
          onSelect={onSelect}
        />
      ))}
    </div>
  );
}

export function ContextUsageHoverCard({
  usage,
  sessionUsage,
}: {
  usage: AcpContextUsage;
  sessionUsage?: AcpSessionUsage | null;
}) {
  const hasLiveUsage = Boolean(sessionUsage && sessionUsage.size > 0);
  const used = hasLiveUsage
    ? sessionUsage!.used
    : usage.input + usage.output + usage.reasoning;
  const limit = hasLiveUsage ? sessionUsage!.size : usage.limit;
  const percent = Math.min(
    100,
    Math.max(0, Math.round((used / Math.max(1, limit)) * 100)),
  );

  const rows: { label: string; value: string }[] = hasLiveUsage
    ? [
        { label: "Used", value: formatCompactTokens(sessionUsage!.used) },
        { label: "Window", value: formatCompactTokens(sessionUsage!.size) },
        ...(sessionUsage!.cost
          ? [
              {
                label: "Cost",
                value: `${sessionUsage!.cost.amount.toFixed(3)} ${sessionUsage!.cost.currency}`,
              },
            ]
          : []),
      ]
    : [
        { label: "Input", value: formatCompactTokens(usage.input) },
        { label: "Output", value: formatCompactTokens(usage.output) },
        { label: "Reasoning", value: formatCompactTokens(usage.reasoning) },
      ];

  return (
    <HoverCard>
      <HoverCardTrigger
        delay={10}
        closeDelay={100}
        render={<Button variant="outline" size="xs" />}
      >
        {percent}% context
      </HoverCardTrigger>
      <HoverCardContent align="end" className="flex w-60 flex-col gap-2.5">
        <Progress value={percent} aria-label="Context used">
          <ProgressLabel>Context</ProgressLabel>
          <span className="ml-auto text-xs text-muted-foreground tabular-nums">
            {formatCompactTokens(used)} / {formatCompactTokens(limit)}
          </span>
        </Progress>
        <div className="flex flex-col gap-1 text-xs">
          {rows.map((row) => (
            <div key={row.label} className="flex justify-between gap-3">
              <span className="text-muted-foreground">{row.label}</span>
              <span className="tabular-nums">{row.value}</span>
            </div>
          ))}
        </div>
      </HoverCardContent>
    </HoverCard>
  );
}

export function SessionArtifactsSheet({
  artifacts,
  onLoadArtifact,
}: {
  artifacts: AcpSessionArtifactSummary[];
  onLoadArtifact: (name: string) => Promise<AcpSessionArtifactContent | null>;
}) {
  const [selectedName, setSelectedName] = React.useState<string>("");
  const [loaded, setLoaded] =
    React.useState<AcpSessionArtifactContent | null>(null);
  const [loading, setLoading] = React.useState(false);

  const paths = React.useMemo(
    () => artifacts.map((item) => item.name),
    [artifacts],
  );

  const handleSelect = React.useCallback(
    async (name: string) => {
      setSelectedName(name);
      setLoading(true);
      try {
        const result = await onLoadArtifact(name);
        setLoaded(result);
      } finally {
        setLoading(false);
      }
    },
    [onLoadArtifact],
  );

  if (artifacts.length === 0) {
    return null;
  }

  return (
    <Sheet
      onOpenChange={(open) => {
        if (open && !selectedName && artifacts[0]) {
          void handleSelect(artifacts[0].name);
        }
      }}
    >
      <SheetTrigger render={<Button variant="outline" size="xs" />}>
        <FileTextIcon />
        Artifacts
        <Badge variant="secondary">{artifacts.length}</Badge>
      </SheetTrigger>
      <SheetContent className="sm:max-w-xl">
        <SheetHeader>
          <SheetTitle>Session Artifacts</SheetTitle>
          <SheetDescription>
            Markdown artifacts generated in this session&apos;s brain directory.
          </SheetDescription>
        </SheetHeader>
        <div className="flex min-h-0 flex-1 flex-col gap-3 px-4 pb-4">
          <FileTree
            paths={paths}
            selected={selectedName}
            onSelect={(path) => void handleSelect(path)}
          />
          <Separator />
          <ScrollArea className="min-h-0 flex-1 rounded-lg border border-border bg-background/60 p-3">
            {loading ? (
              <div className="flex items-center justify-center py-8">
                <Spinner />
              </div>
            ) : loaded ? (
              <SessionMessageBody text={loaded.content} />
            ) : (
              <p className="text-xs text-muted-foreground">
                Select an artifact above to inspect its contents.
              </p>
            )}
          </ScrollArea>
        </div>
      </SheetContent>
    </Sheet>
  );
}
