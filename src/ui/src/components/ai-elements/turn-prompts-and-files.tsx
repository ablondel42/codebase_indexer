import * as React from "react";
import {
  ChevronRightIcon,
  FileIcon,
  FolderIcon,
  TerminalIcon,
} from "lucide-react";

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
import {
  buildFileTree,
  formatCompactTokens,
  type FileTreeNode,
} from "@/lib/acp/turn-grouping";
import type {
  AcpPendingConfirmation,
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
  sessionUsage,
}: {
  sessionUsage?: AcpSessionUsage | null;
}) {
  if (!sessionUsage || sessionUsage.size <= 0) {
    return null;
  }
  const used = sessionUsage.used;
  const limit = sessionUsage.size;
  const percent = Math.min(
    100,
    Math.max(0, Math.round((used / limit) * 100)),
  );

  const rows: { label: string; value: string }[] = [
    { label: "Used", value: used.toLocaleString() },
    { label: "Window", value: limit.toLocaleString() },
    ...(sessionUsage.cost
      ? [
          {
            label: "Cost",
            value: `${sessionUsage.cost.amount.toFixed(3)} ${sessionUsage.cost.currency}`,
          },
        ]
      : []),
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
