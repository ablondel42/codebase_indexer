import { useState, type FormEvent } from "react";
import {
  CheckCircle2,
  ChevronDown,
  KeyRound,
  Loader2,
  LogIn,
  LogOut,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  DEFAULT_AUTH_METHOD_ID,
  type AcpAuthenticateOptions,
  type AcpAuthMethod,
  type AcpAuthStatus,
} from "@/lib/acp/types";

interface AcpAuthControlsProps {
  authStatus: AcpAuthStatus;
  authMethods: AcpAuthMethod[];
  activeMethodId: string | null;
  authenticatingMethodId: string | null;
  canLogout: boolean;
  authError: string | null;
  onClearAuthError: () => void;
  onAuthenticate: (
    methodId?: string,
    options?: AcpAuthenticateOptions,
  ) => Promise<void>;
  onLogout: () => Promise<void>;
  onCancelAuth: () => void;
}

export function AcpAuthControls({
  authStatus,
  authMethods,
  activeMethodId,
  authenticatingMethodId,
  canLogout,
  authError,
  onClearAuthError,
  onAuthenticate,
  onLogout,
  onCancelAuth,
}: AcpAuthControlsProps) {
  const [showApiKeyInput, setShowApiKeyInput] = useState(false);
  const [apiKeyDraft, setApiKeyDraft] = useState("");

  const googleMethod =
    authMethods.find((method) => method.id === DEFAULT_AUTH_METHOD_ID) ??
    authMethods[0] ??
    null;

  const activeMethod = authMethods.find(
    (method) => method.id === activeMethodId,
  );
  const inFlightMethod = authMethods.find(
    (method) => method.id === authenticatingMethodId,
  );

  const handleSelectMethod = (method: AcpAuthMethod) => {
    if (method.id === "gemini-api-key") {
      setShowApiKeyInput(true);
      return;
    }
    setShowApiKeyInput(false);
    setApiKeyDraft("");
    void onAuthenticate(method.id);
  };

  const handleApiKeySubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const submittedKey = apiKeyDraft;
    setApiKeyDraft("");
    setShowApiKeyInput(false);
    void onAuthenticate(
      "gemini-api-key",
      submittedKey.trim() ? { apiKey: submittedKey } : undefined,
    );
  };

  if (authStatus === "checking") {
    return (
      <span className="inline-flex items-center gap-1.5 text-muted-foreground">
        <Loader2 className="size-3.5 animate-spin" />
        Checking auth…
      </span>
    );
  }

  if (authStatus === "authenticating" || authStatus === "logging_out") {
    const isBrowserOAuth =
      authenticatingMethodId === "oauth-personal" ||
      authenticatingMethodId === "oauth-business";
    return (
      <div className="flex items-center gap-2">
        <span className="inline-flex items-center gap-1.5 rounded-md border border-border bg-muted/40 px-2.5 py-1 text-foreground">
          <Loader2 className="size-3.5 animate-spin text-amber-500" />
          {authStatus === "logging_out"
            ? "Logging out…"
            : isBrowserOAuth
              ? `Complete ${inFlightMethod?.name ?? "Google login"} in browser…`
              : `Authenticating (${inFlightMethod?.name ?? authenticatingMethodId})…`}
        </span>
        {authStatus === "authenticating" && (
          <Button variant="ghost" size="sm" onClick={onCancelAuth}>
            Cancel
          </Button>
        )}
      </div>
    );
  }

  return (
    <div className="flex items-center gap-2">
      {showApiKeyInput ? (
        <form onSubmit={handleApiKeySubmit} className="flex items-center gap-1.5">
          <KeyRound className="size-3.5 text-muted-foreground" />
          <input
            type="password"
            value={apiKeyDraft}
            onChange={(event) => setApiKeyDraft(event.target.value)}
            placeholder="Gemini API key (or blank for env)"
            autoComplete="off"
            className="h-7 w-52 rounded-md border border-input bg-background px-2 text-xs text-foreground outline-none focus-visible:border-ring"
          />
          <Button type="submit" size="sm">
            Apply
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => {
              setApiKeyDraft("");
              setShowApiKeyInput(false);
            }}
          >
            Cancel
          </Button>
        </form>
      ) : authStatus === "authenticated" ? (
        <DropdownMenu>
          <DropdownMenuTrigger
            render={<Button variant="outline" size="sm" />}
          >
            <CheckCircle2 className="size-3.5 text-emerald-500" />
            <span>
              Authenticated
              {activeMethod ? ` · ${activeMethod.name}` : ""}
            </span>
            <ChevronDown className="size-3.5 text-muted-foreground" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-80">
            <DropdownMenuGroup>
              <DropdownMenuLabel>Switch Authentication Method</DropdownMenuLabel>
              {authMethods.map((method) => (
                <DropdownMenuItem
                  key={method.id}
                  onClick={() => handleSelectMethod(method)}
                  className="flex flex-col items-start gap-0.5 py-1.5"
                >
                  <span className="font-medium">{method.name}</span>
                  {method.description && (
                    <span className="text-xs text-muted-foreground whitespace-normal">
                      {method.description}
                    </span>
                  )}
                </DropdownMenuItem>
              ))}
            </DropdownMenuGroup>
            {canLogout && (
              <>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  variant="destructive"
                  onClick={() => void onLogout()}
                >
                  <LogOut className="size-3.5" />
                  Log out
                </DropdownMenuItem>
              </>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      ) : (
        <div className="flex items-center gap-1.5">
          {googleMethod && (
            <Button
              size="sm"
              onClick={() => handleSelectMethod(googleMethod)}
            >
              <LogIn className="size-3.5" />
              {googleMethod.name}
            </Button>
          )}
          {authMethods.length > 0 && (
            <DropdownMenu>
              <DropdownMenuTrigger
                render={<Button variant="outline" size="sm" />}
              >
                <span>Auth methods</span>
                <ChevronDown className="size-3.5 text-muted-foreground" />
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-80">
                <DropdownMenuGroup>
                  <DropdownMenuLabel>
                    Select Authentication Method
                  </DropdownMenuLabel>
                  {authMethods.map((method) => (
                    <DropdownMenuItem
                      key={method.id}
                      onClick={() => handleSelectMethod(method)}
                      className="flex flex-col items-start gap-0.5 py-1.5"
                    >
                      <span className="font-medium">{method.name}</span>
                      {method.description && (
                        <span className="text-xs text-muted-foreground whitespace-normal">
                          {method.description}
                        </span>
                      )}
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuGroup>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>
      )}

      {authError && (
        <div
          className="flex max-w-xs items-center gap-1 rounded-md border border-destructive/40 bg-destructive/10 px-2 py-1 text-destructive"
          title={authError}
        >
          <span className="truncate">{authError}</span>
          <button
            type="button"
            onClick={onClearAuthError}
            className="shrink-0 rounded p-0.5 hover:bg-destructive/20"
            aria-label="Dismiss authentication error"
          >
            <X className="size-3" />
          </button>
        </div>
      )}
    </div>
  );
}
