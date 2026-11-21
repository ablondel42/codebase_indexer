import { useCallback, useEffect, useRef, useState } from "react";

import { AcpBrowserClient } from "@/lib/acp/client";
import {
  DEFAULT_AUTH_METHOD_ID,
  type AcpAuthenticateOptions,
  type AcpAuthStatus,
  type AcpConnectionStatus,
  type AcpInitializeResult,
} from "@/lib/acp/types";

export interface UseAcpConnectionState {
  status: AcpConnectionStatus;
  result: AcpInitializeResult | null;
  authStatus: AcpAuthStatus;
  activeMethodId: string | null;
  authenticatingMethodId: string | null;
  canLogout: boolean;
  error: string | null;
  authError: string | null;
  clearAuthError: () => void;
  authenticate: (
    methodId?: string,
    options?: AcpAuthenticateOptions,
  ) => Promise<void>;
  logout: () => Promise<void>;
  reconnect: () => void;
}

export function useAcpConnection(): UseAcpConnectionState {
  const clientRef = useRef<AcpBrowserClient | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [status, setStatus] = useState<AcpConnectionStatus>("initializing");
  const [result, setResult] = useState<AcpInitializeResult | null>(null);
  const [authStatus, setAuthStatus] = useState<AcpAuthStatus>("checking");
  const [activeMethodId, setActiveMethodId] = useState<string | null>(null);
  const [authenticatingMethodId, setAuthenticatingMethodId] = useState<
    string | null
  >(null);
  const [error, setError] = useState<string | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);

  const clearAuthError = useCallback(() => {
    setAuthError(null);
  }, []);

  const reconnect = useCallback(() => {
    setStatus("initializing");
    setAuthStatus("checking");
    setError(null);
    setAuthError(null);
    setActiveMethodId(null);
    setAuthenticatingMethodId(null);
    setAttempt((current) => current + 1);
  }, []);

  useEffect(() => {
    let disposed = false;
    const client = new AcpBrowserClient();
    clientRef.current = client;

    client
      .connectAndInitialize()
      .then(async (initResult) => {
        if (disposed) {
          return;
        }
        setResult(initResult);
        setStatus("ready");
        const initialAuth = await client.probeAuthStatus();
        if (!disposed) {
          setAuthStatus(initialAuth);
        }
      })
      .catch((err: unknown) => {
        if (disposed) {
          return;
        }
        const message =
          err instanceof Error ? err.message : "Failed to initialize ACP.";
        setResult(null);
        setError(message);
        setStatus("error");
      });

    return () => {
      disposed = true;
      if (clientRef.current === client) {
        clientRef.current = null;
      }
      client.close();
    };
  }, [attempt]);

  const authenticate = useCallback(
    async (
      methodId: string = DEFAULT_AUTH_METHOD_ID,
      options?: AcpAuthenticateOptions,
    ) => {
      const client = clientRef.current;
      if (!client) {
        return;
      }
      setAuthError(null);
      setAuthenticatingMethodId(methodId);
      setAuthStatus("authenticating");
      try {
        await client.authenticate(methodId, options);
        setActiveMethodId(methodId);
        setAuthenticatingMethodId(null);
        setAuthStatus("authenticated");
      } catch (err: unknown) {
        const message =
          err instanceof Error ? err.message : "ACP authentication failed.";
        setAuthenticatingMethodId(null);
        setAuthStatus("unauthenticated");
        setAuthError(message);
      }
    },
    [],
  );

  const logout = useCallback(async () => {
    const client = clientRef.current;
    if (!client) {
      return;
    }
    setAuthError(null);
    setAuthStatus("logging_out");
    try {
      await client.logout();
      setActiveMethodId(null);
      setAuthStatus("unauthenticated");
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : "Failed to log out from ACP.";
      setAuthStatus("authenticated");
      setAuthError(message);
    }
  }, []);

  const canLogout = Boolean(result?.agentCapabilities?.auth?.logout);

  return {
    status,
    result,
    authStatus,
    activeMethodId,
    authenticatingMethodId,
    canLogout,
    error,
    authError,
    clearAuthError,
    authenticate,
    logout,
    reconnect,
  };
}
