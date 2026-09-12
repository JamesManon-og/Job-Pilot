"use client";

import { useCallback, useEffect, useState } from "react";
import { describeError } from "@/lib/api";

interface ApiState<T> {
  source: unknown; // the fetcher that produced this result
  data: T | null;
  error: string | null;
}

/**
 * Run `fetcher` (memoize it with useCallback) and track its result.
 *
 * Responses from superseded requests are dropped, so switching tabs or dragging
 * a slider can't show one request's data under another filter. While a new
 * fetcher is loading, the previous result is hidden rather than shown as stale.
 * `reload()` refetches in place without hiding the current data.
 */
export function useApi<T>(fetcher: () => Promise<T>) {
  const [state, setState] = useState<ApiState<T>>({ source: null, data: null, error: null });
  const [version, setVersion] = useState(0);

  useEffect(() => {
    let cancelled = false;
    fetcher().then(
      (data) => {
        if (!cancelled) setState({ source: fetcher, data, error: null });
      },
      (error: unknown) => {
        if (!cancelled) {
          setState((prev) => ({
            source: fetcher,
            data: prev.source === fetcher ? prev.data : null,
            error: describeError(error),
          }));
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [fetcher, version]);

  const reload = useCallback(() => setVersion((v) => v + 1), []);
  const current = state.source === fetcher;
  return {
    data: current ? state.data : null,
    error: current ? state.error : null,
    loading: !current,
    reload,
  };
}
