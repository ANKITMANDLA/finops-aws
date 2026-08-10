import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { api, ApiError } from "@/lib/api";
import { useApi, useInterval } from "@/lib/hooks";
import type { Health, ScanDetail, ScanJob, ScanMeta } from "@/lib/types";

interface ScanContextValue {
  /** The id the views should query. "latest" until the user picks a specific scan. */
  scanId: string;
  selectScan: (scanId: string) => void;
  scan: ScanDetail | null;
  scans: ScanMeta[];
  health: Health | null;
  loading: boolean;
  error: ApiError | null;
  job: ScanJob | null;
  scanning: boolean;
  startScan: () => Promise<void>;
  startError: string | null;
  refresh: () => void;
}

const ScanContext = createContext<ScanContextValue | null>(null);

export function ScanProvider({ children }: { children: ReactNode }) {
  const [scanId, setScanId] = useState("latest");
  const [version, setVersion] = useState(0);
  const [job, setJob] = useState<ScanJob | null>(null);
  const [startError, setStartError] = useState<string | null>(null);

  const health = useApi<Health>(() => api.health(), [version]);
  const scans = useApi<ScanMeta[]>(() => api.listScans(50), [version]);
  const scan = useApi<ScanDetail>(() => api.scan(scanId), [scanId, version]);

  const refresh = useCallback(() => setVersion((value) => value + 1), []);

  const scanning = job?.status === "running" || job?.status === "queued";

  // Poll while a scan is in flight so the header stage updates without a reload.
  useInterval(
    () => {
      api
        .scanStatus()
        .then((status) => {
          setJob(status);
          if (status.status === "succeeded" || status.status === "failed") {
            refresh();
          }
        })
        .catch(() => undefined);
    },
    2000,
    scanning,
  );

  // One status read on mount picks up a scan started from the CLI or another tab.
  useEffect(() => {
    api
      .scanStatus()
      .then((status) => setJob(status.status === "idle" ? null : status))
      .catch(() => undefined);
  }, []);

  const startScan = useCallback(async () => {
    setStartError(null);
    try {
      const started = await api.startScan();
      setJob({ ...started, status: "running" });
    } catch (cause) {
      setStartError(cause instanceof ApiError ? cause.message : "Could not start the scan");
    }
  }, []);

  const selectScan = useCallback((next: string) => setScanId(next), []);

  const value = useMemo<ScanContextValue>(
    () => ({
      scanId,
      selectScan,
      scan: scan.data,
      scans: scans.data ?? [],
      health: health.data,
      loading: scan.loading,
      error: scan.error,
      job,
      scanning,
      startScan,
      startError,
      refresh,
    }),
    [
      scanId,
      selectScan,
      scan.data,
      scan.loading,
      scan.error,
      scans.data,
      health.data,
      job,
      scanning,
      startScan,
      startError,
      refresh,
    ],
  );

  return <ScanContext.Provider value={value}>{children}</ScanContext.Provider>;
}

export function useScanContext(): ScanContextValue {
  const context = useContext(ScanContext);
  if (!context) throw new Error("useScanContext must be used inside <ScanProvider>");
  return context;
}

/** Convenience for views that only need the id they should query. */
export function useScanId(): string {
  return useScanContext().scanId;
}
