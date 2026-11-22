"use client";

import { Button } from "@/components/ui/primitives";
import type { LiveRefresh } from "@/lib/useLiveRefresh";

/**
 * The header indicator of a list that changes on its own: a pulsing dot and
 * "Updating live" while work is in progress, the time of the last update
 * otherwise, and a Refresh button either way. It goes in a Panel's `aside`, so
 * every progressing list says the same thing in the same place.
 */
export function LiveStatus({
  live,
  updatedAt,
  refresh,
}: Pick<LiveRefresh, "live" | "updatedAt" | "refresh">) {
  const time = updatedAt ? updatedAt.toLocaleTimeString() : null;
  return (
    <span className="flex items-center gap-2 text-xs text-muted">
      {live ? (
        <>
          <span className="relative flex h-2 w-2" aria-hidden="true">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary opacity-60" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-primary" />
          </span>
          <span>Updating live{time ? ` · ${time}` : ""}</span>
        </>
      ) : (
        <span>{time ? `Updated ${time}` : "Loading…"}</span>
      )}
      <Button variant="ghost" size="sm" onClick={() => void refresh()}>
        Refresh
      </Button>
    </span>
  );
}
