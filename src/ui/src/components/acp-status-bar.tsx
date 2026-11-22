import {
  CircleAlertIcon,
  PanelRightIcon,
  RefreshCwIcon,
  ShieldCheckIcon,
  SparklesIcon,
} from "lucide-react";

import { AcpAuthControls } from "@/components/acp-auth-controls";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Spinner } from "@/components/ui/spinner";
import { Toggle } from "@/components/ui/toggle";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import type {
  AcpAntigravityTier,
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
  accountTier?: AcpAntigravityTier | null;
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

export function AcpStatusBar({
  status,
  result,
  authStatus,
  activeMethodId,
  authenticatingMethodId,
  canLogout,
  accountTier,
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
  const isConnecting = status === "connecting" || status === "initializing";

  return (
    <div className="flex min-w-0 flex-wrap items-center gap-2">
      {status === "ready" && result ? (
        <div className="flex items-center gap-1.5">
          <Badge variant="outline" className="h-7 gap-1.5 px-2.5">
            <ShieldCheckIcon className="text-emerald-500" />
            <span>
              {agentName}
              {agentVersion}
            </span>
          </Badge>
          <Badge variant="secondary">ACP v{result.protocolVersion}</Badge>
        </div>
      ) : status === "error" ? (
        <Tooltip>
          <TooltipTrigger
            render={
              <Badge variant="destructive" className="h-7 max-w-xs gap-1.5 px-2.5" />
            }
          >
            <CircleAlertIcon />
            <span className="truncate">{error ?? "ACP connection error"}</span>
          </TooltipTrigger>
          <TooltipContent>
            <p>{error ?? "ACP connection error"}</p>
          </TooltipContent>
        </Tooltip>
      ) : (
        <Badge variant="outline" className="h-7 gap-1.5 px-2.5 text-muted-foreground">
          <Spinner className="size-3" />
          <span>Connecting to ACP…</span>
        </Badge>
      )}

      {status === "ready" && result && (
        <>
          <Separator orientation="vertical" className="my-1.5" />
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
        </>
      )}

      {status === "ready" && authStatus === "authenticated" && (
        <>
          {accountTier && (
            <Tooltip>
              <TooltipTrigger
                render={
                  <Badge
                    variant="secondary"
                    className="h-7 gap-1.5 px-2.5"
                  />
                }
              >
                <SparklesIcon className="size-3.5 text-amber-500" />
                <span>{accountTier.name}</span>
              </TooltipTrigger>
              <TooltipContent className="flex flex-col gap-1">
                <p>Tier: {accountTier.id}</p>
                {accountTier.upgradeUri && (
                  <a
                    href={accountTier.upgradeUri}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="underline"
                  >
                    Manage plan
                  </a>
                )}
              </TooltipContent>
            </Tooltip>
          )}
          <Separator orientation="vertical" className="my-1.5" />
          <Toggle
            variant="outline"
            size="sm"
            pressed={sessionsPanelOpen}
            onPressedChange={() => onToggleSessionsPanel()}
            aria-label="Toggle sessions window"
          >
            <PanelRightIcon />
            <span>Sessions</span>
            <Badge variant="secondary">{sessionCount}</Badge>
            {activeSessionId && <Badge variant="default">Active</Badge>}
          </Toggle>
        </>
      )}

      <Tooltip>
        <TooltipTrigger
          render={
            <Button
              variant="outline"
              size="sm"
              onClick={onReconnect}
              disabled={isConnecting}
            />
          }
        >
          {isConnecting ? <Spinner className="size-3.5" /> : <RefreshCwIcon />}
          <span>Reconnect</span>
        </TooltipTrigger>
        <TooltipContent>
          <p>Reconnect to ACP WebSocket bridge</p>
        </TooltipContent>
      </Tooltip>
    </div>
  );
}
