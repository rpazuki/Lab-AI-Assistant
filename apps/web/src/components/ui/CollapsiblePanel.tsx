"use client";

import { type ReactNode, useEffect, useId, useState } from "react";

import { Icon } from "@/components/ui/icons";

/** A choice that cannot be stored is simply not kept, as in the sidebar. */
function remembered(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function remember(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Not kept.
  }
}

/**
 * A panel that folds away to its title bar, like the navigation sidebar, and
 * remembers being folded on this browser. Folded, its content stays mounted and
 * keeps its page, filters and refresh; it is only hidden.
 */
export function CollapsiblePanel({
  title,
  storageKey,
  count,
  aside,
  flush = false,
  children,
}: {
  title: string;
  /** Where the folded state is kept, e.g. "collections.candidates". */
  storageKey: string;
  /** What the list holds, shown beside the title: "3,420 candidates". */
  count?: ReactNode;
  aside?: ReactNode;
  flush?: boolean;
  children: ReactNode;
}) {
  const key = `lab.panel.${storageKey}.collapsed`;
  const [collapsed, setCollapsed] = useState(false);
  const bodyId = useId();
  useEffect(() => {
    setCollapsed(remembered(key) === "1");
  }, [key]);
  function toggle() {
    const next = !collapsed;
    setCollapsed(next);
    remember(key, next ? "1" : "0");
  }
  return (
    <section className="overflow-hidden rounded-card border border-border bg-surface shadow-card">
      <header
        className={`flex min-h-14 flex-wrap items-center justify-between gap-x-4 gap-y-2 px-5 py-3 ${
          collapsed ? "" : "border-b border-border"
        }`}
      >
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <button
            type="button"
            onClick={toggle}
            aria-expanded={!collapsed}
            aria-controls={bodyId}
            aria-label={`${collapsed ? "Show" : "Hide"} ${title}`}
            className="rounded-control p-1 text-muted transition-colors hover:bg-surface-subtle hover:text-heading"
          >
            <span className={`inline-block transition-transform ${collapsed ? "-rotate-90" : ""}`}>
              <Icon name="chevron" />
            </span>
          </button>
          <h2 className="text-card-title font-semibold text-heading">{title}</h2>
          {count}
        </div>
        {collapsed ? null : aside}
      </header>
      <div id={bodyId} hidden={collapsed} className={flush ? "" : "p-5"}>
        {children}
      </div>
    </section>
  );
}
