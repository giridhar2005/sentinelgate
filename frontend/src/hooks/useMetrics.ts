import { useState, useEffect, useCallback, useRef } from "react";
import { MetricsSummary, ReplayPayload, ReplayResult, ChaosConfig } from "../types";

export function useMetrics() {
  const [metrics, setMetrics] = useState<MetricsSummary | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [isLive, setIsLive] = useState<boolean>(true);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const pollTimerRef = useRef<number | null>(null);

  const fetchMetrics = useCallback(async () => {
    try {
      const res = await fetch("/api/metrics");
      if (!res.ok) {
        throw new Error(`HTTP error ${res.status}`);
      }
      const data: MetricsSummary = await res.json();
      setMetrics(data);
      setLastUpdated(new Date());
      setError(null);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Failed to fetch metrics";
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchMetrics();
  }, [fetchMetrics]);

  useEffect(() => {
    if (!isLive) {
      if (pollTimerRef.current) {
        clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
      return;
    }

    pollTimerRef.current = window.setInterval(() => {
      fetchMetrics();
    }, 2000);

    return () => {
      if (pollTimerRef.current) {
        clearInterval(pollTimerRef.current);
      }
    };
  }, [isLive, fetchMetrics]);

  const toggleChaos = useCallback(
    async (body?: Partial<ChaosConfig>) => {
      try {
        const res = await fetch("/api/chaos", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: body ? JSON.stringify(body) : undefined,
        });
        if (!res.ok) {
          throw new Error(`Failed to toggle chaos: ${res.statusText}`);
        }
        const data = await res.json();
        // Immediately refresh metrics to sync UI
        await fetchMetrics();
        return data;
      } catch (err) {
        console.error("Chaos toggle failed:", err);
        throw err;
      }
    },
    [fetchMetrics]
  );

  const runShadowReplay = useCallback(async (payload: ReplayPayload): Promise<ReplayResult> => {
    const res = await fetch("/api/replay", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      throw new Error(errData.error || `Replay failed with HTTP ${res.status}`);
    }
    return await res.json();
  }, []);

  return {
    metrics,
    loading,
    error,
    isLive,
    setIsLive,
    lastUpdated,
    refresh: fetchMetrics,
    toggleChaos,
    runShadowReplay,
  };
}
