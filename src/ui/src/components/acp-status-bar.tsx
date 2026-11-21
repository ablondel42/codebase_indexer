import { RefreshCw, ShieldCheck } from "lucide-react";

import { Button } from "@/components/ui/button";
import type {
  AcpConnectionStatus,
  AcpInitializeResult,
} from "@/lib/acp/types";

interface AcpStatusBarProps {
  status: AcpConnectionStatus;
  result: AcpInitializeResult | null;
  error: string | null;
  onReconnect: () => void;
}

const STATUS_DOT_STYLES: Record<AcpConnectionStatus, string> = {
  idle: "bg-muted-foreground",
  connecting: "bg-amber-500 animate-pulse",
  initializing: "bg-amber-500 animate-pulse",
  ready: "bg-emerald-500",
  error: "bg-destructive",
};

export function AcpStatusBar({
  status,
  result,
  error,
  onReconnect,
}: AcpStatusBarProps) {
  const agentName =
    result?.agentInfo?.title || result?.agentInfo?.name || "agy acp";
  const agentVersion = result?.agentInfo?.version
    ? ` v${result.agentInfo.version}`
    : "";
  const authCount = result?.authMethods?.length ?? 0;

  return (
    <div className="flex items-center gap-3 text-xs">
      <div className="flex items-center gap-2 rounded-md border border-border bg-muted/40 px-2.5 py-1">
        <span
          className={`size-2 rounded-full ${STATUS_DOT_STYLES[status]}`}
          aria-hidden="true"
        />
        {status === "ready" && result ? (
          <div className="flex items-center gap-2">
            <ShieldCheck className="size-3.5 text-emerald-500" />
            <span className="font-medium text-foreground">
              {agentName}
              {agentVersion}
            </span>
            <span className="rounded bg-background px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
              ACP v{result.protocolVersion}
            </span>
            <span className="hidden text-muted-foreground sm:inline">
              {authCount} auth {authCount === 1 ? "method" : "methods"}
            </span>
          </div>
        ) : status === "error" ? (
          <span className="max-w-md truncate text-destructive" title={error ?? ""}>
            {error ?? "ACP connection error"}
          </span>
        ) : (
          <span className="text-muted-foreground">Connecting to ACP…</span>
        )}
      </div>
      <Button
        variant="outline"
        size="sm"
        onClick={onReconnect}
        disabled={status === "connecting" || status === "initializing"}
      >
        <RefreshCw className="size-3.5" />
        Reconnect
      </Button>
    </div>
  );
}
