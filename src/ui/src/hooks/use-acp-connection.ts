import { useCallback, useEffect, useState } from "react";

import { AcpBrowserClient } from "@/lib/acp/client";
import type {
  AcpConnectionStatus,
  AcpInitializeResult,
} from "@/lib/acp/types";

export interface UseAcpConnectionState {
  status: AcpConnectionStatus;
  result: AcpInitializeResult | null;
  error: string | null;
  reconnect: () => void;
}

export function useAcpConnection(): UseAcpConnectionState {
  const [attempt, setAttempt] = useState(0);
  const [status, setStatus] = useState<AcpConnectionStatus>("initializing");
  const [result, setResult] = useState<AcpInitializeResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reconnect = useCallback(() => {
    setStatus("initializing");
    setError(null);
    setAttempt((current) => current + 1);
  }, []);

  useEffect(() => {
    let disposed = false;
    const client = new AcpBrowserClient();

    client
      .connectAndInitialize()
      .then((initResult) => {
        if (disposed) {
          return;
        }
        setResult(initResult);
        setStatus("ready");
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
      client.close();
    };
  }, [attempt]);

  return { status, result, error, reconnect };
}
