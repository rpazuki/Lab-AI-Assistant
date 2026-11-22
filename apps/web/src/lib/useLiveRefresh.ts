"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/** How often a list is asked again while something in it is still changing. */
export const LIVE_INTERVAL_MS = 3000;
/**
 * How long a list keeps asking after the user changed something. Some work an
 * action starts leaves no in-progress state to watch: freezing a run is final, yet
 * its candidates appear only once reconciliation has run on the worker.
 */
export const SETTLE_MS = 60_000;

export interface LiveRefresh {
  /** Asking again on a short interval: something is in progress. */
  live: boolean;
  /** When the last successful load finished; null before the first. */
  updatedAt: Date | null;
  /** Load now, outside the schedule (a Refresh button). */
  refresh: () => Promise<void>;
  /** Load now, then keep asking for SETTLE_MS: call after any change the user makes. */
  kick: () => void;
}

/**
 * The one way a page follows work that progresses on the worker queue.
 *
 * Every list with in-progress states (discovery runs, acquisition plans, release
 * indexing, extraction and evaluation runs, the task queue) loads through this, so
 * each behaves the same way:
 *
 * - loads once on mount;
 * - asks again every LIVE_INTERVAL_MS while `active` says something is in progress,
 *   and for SETTLE_MS after `kick()`;
 * - otherwise asks every `idleMs`, or not at all when it is null;
 * - never overlaps two loads, pauses while the tab is hidden, and loads at once
 *   when the tab is shown again.
 *
 * It polls the same list endpoints the page already reads, rather than holding a
 * server push channel open: the state shown is always the row's own state, and a
 * dropped connection cannot leave a page silently stale.
 *
 * `load` should catch and report its own errors; one that throws counts as not
 * updated, and the schedule carries on.
 */
export function useLiveRefresh(
  load: () => Promise<void>,
  { active, idleMs = null }: { active: boolean; idleMs?: number | null },
): LiveRefresh {
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [settleUntil, setSettleUntil] = useState(0);
  const [settling, setSettling] = useState(false);
  const loadRef = useRef(load);
  loadRef.current = load;
  const inFlight = useRef<Promise<void> | null>(null);

  const refresh = useCallback(() => {
    if (inFlight.current) return inFlight.current;
    const run = (async () => {
      try {
        await loadRef.current();
        setUpdatedAt(new Date());
      } catch {
        // The load reports its own failure; the schedule simply tries again.
      } finally {
        inFlight.current = null;
      }
    })();
    inFlight.current = run;
    return run;
  }, []);

  const kick = useCallback(() => {
    setSettleUntil(Date.now() + SETTLE_MS);
    setSettling(true);
    void refresh();
  }, [refresh]);

  // The settle window ends on its own, so `live` turns off without a load.
  useEffect(() => {
    if (!settling) return;
    const timer = window.setTimeout(
      () => setSettling(false),
      Math.max(0, settleUntil - Date.now()),
    );
    return () => window.clearTimeout(timer);
  }, [settling, settleUntil]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const live = active || settling;
  const delay = live ? LIVE_INTERVAL_MS : idleMs;

  useEffect(() => {
    if (delay === null) return;
    const timer = window.setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      void refresh();
    }, delay);
    return () => window.clearInterval(timer);
  }, [delay, refresh]);

  useEffect(() => {
    function shown() {
      if (!document.hidden) void refresh();
    }
    document.addEventListener("visibilitychange", shown);
    return () => document.removeEventListener("visibilitychange", shown);
  }, [refresh]);

  return { live, updatedAt, refresh, kick };
}
