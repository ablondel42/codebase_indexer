import { PanelRight, RefreshCw, ShieldCheck } from "lucide-react";

import { AcpAuthControls } from "@/components/acp-auth-controls";
import { Button } from "@/components/ui/button";
import type {
  AcpAuthenticateOptions,
  AcpAuthStatus,
  AcpConnectionStatus,
  AcpInitializeResult,
} from "@/lib/acp/types";

interface AcpStatusBarProps {
  status: AcpConnectionStatus;
  result: AcpInitializeResult | null;
  authStatus: AcpAuthStatus;
  activeMethodId: string | null;
  authenticatingMethodId: string | null;
  canLogout: boolean;
  sessionCount: number;
  activeSessionId: string | null;
  sessionsPanelOpen: boolean;
  onToggleSessionsPanel: () => void;
  error: string | null;
  authError: string | null;
  onClearAuthError: () => void;
  onAuthenticate: (
    methodId?: string,
    options?: AcpAuthenticateOptions,
  ) => Promise<void>;
  onLogout: () => Promise<void>;
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
  authStatus,
  activeMethodId,
  authenticatingMethodId,
  canLogout,
  sessionCount,
  activeSessionId,
  sessionsPanelOpen,
  onToggleSessionsPanel,
  error,
  authError,
  onClearAuthError,
  onAuthenticate,
  onLogout,
  onReconnect,
}: AcpStatusBarProps) {
  const agentName =
    result?.agentInfo?.title || result?.agentInfo?.name || "agy acp";
  const agentVersion = result?.agentInfo?.version
    ? ` v${result.agentInfo.version}`
    : "";

  return (
    <div className="flex flex-wrap items-center gap-2.5 text-xs">
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
          </div>
        ) : status === "error" ? (
          <span
            className="max-w-md truncate text-destructive"
            title={error ?? ""}
          >
            {error ?? "ACP connection error"}
          </span>
        ) : (
          <span className="text-muted-foreground">Connecting to ACP…</span>
        )}
      </div>

      {status === "ready" && result && (
        <AcpAuthControls
          authStatus={authStatus}
          authMethods={result.authMethods ?? []}
          activeMethodId={activeMethodId}
          authenticatingMethodId={authenticatingMethodId}
          canLogout={canLogout}
          authError={authError}
          onClearAuthError={onClearAuthError}
          onAuthenticate={onAuthenticate}
          onLogout={onLogout}
          onCancelAuth={onReconnect}
        />
      )}

      {status === "ready" && authStatus === "authenticated" && (
        <Button
          variant={sessionsPanelOpen ? "secondary" : "outline"}
          size="sm"
          onClick={onToggleSessionsPanel}
        >
          <PanelRight className="size-3.5" />
          Sessions ({sessionCount})
          {activeSessionId ? " · Active" : ""}
        </Button>
      )}

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
