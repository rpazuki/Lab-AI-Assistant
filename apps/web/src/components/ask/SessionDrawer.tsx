"use client";

/**
 * The list of conversations, as a drawer that floats over the page.
 *
 * Closed, it takes no room: the conversation and its sources have the width.
 * The **Sessions** button opens it; choosing a conversation, the close button,
 * Escape or a click outside closes it again.
 */

import { useEffect, useRef } from "react";

import { Pager } from "@/components/ui/Pager";
import { Icon } from "@/components/ui/icons";
import { Empty } from "@/components/ui/primitives";
import type { Pager as PagerState } from "@/lib/usePager";
import type { ChatSession } from "@/types";

export function SessionDrawer({
  open,
  onClose,
  sessions,
  total,
  nextCursor,
  pager,
  currentId,
  onChoose,
  onRename,
  onDelete,
}: {
  open: boolean;
  onClose: () => void;
  sessions: ChatSession[];
  total: number;
  nextCursor: string | null;
  pager: PagerState;
  currentId: string | null;
  onChoose: (session: ChatSession) => void;
  onRename: (session: ChatSession) => void;
  onDelete: (session: ChatSession) => void;
}) {
  const panel = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!open) return;
    panel.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <>
      {/* The page behind: a click there closes the drawer. */}
      <button
        type="button"
        aria-label="Close sessions"
        tabIndex={-1}
        className="fixed inset-0 z-30 cursor-default bg-primary/10"
        onClick={onClose}
      />
      <aside
        ref={panel}
        tabIndex={-1}
        role="dialog"
        aria-label="Sessions"
        className="absolute left-0 top-full z-40 mt-2 flex max-h-[70vh] w-[26rem] max-w-[calc(100vw-2rem)] flex-col overflow-hidden rounded-card border border-border bg-surface shadow-card outline-none"
      >
        <header className="flex items-center justify-between border-b border-border px-4 py-3">
          <h2 className="text-card-title font-semibold text-heading">Sessions</h2>
          <button
            type="button"
            aria-label="Close"
            className="rounded-control p-1 text-muted hover:bg-surface-subtle hover:text-heading"
            onClick={onClose}
          >
            <Icon name="close" />
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {sessions.length === 0 ? (
            <div className="px-4 py-3">
              <Empty>No sessions yet.</Empty>
            </div>
          ) : (
            <ul className="space-y-0.5 p-2 text-sm">
              {sessions.map((session) => (
                <li key={session.id} className="group flex items-center gap-1">
                  <button
                    type="button"
                    className={`flex-1 truncate rounded-control border-l-2 px-3 py-1.5 text-left transition-colors ${
                      session.id === currentId
                        ? "border-accent bg-success-lt font-medium text-heading"
                        : "border-transparent text-ink hover:bg-surface-subtle"
                    }`}
                    onClick={() => onChoose(session)}
                  >
                    {session.title ?? "Untitled session"}
                  </button>
                  <button
                    type="button"
                    aria-label={`Rename ${session.title ?? "session"}`}
                    className="rounded-badge px-1 text-xs text-muted hover:text-heading"
                    onClick={() => onRename(session)}
                  >
                    rename
                  </button>
                  <button
                    type="button"
                    aria-label={`Delete ${session.title ?? "session"}`}
                    className="rounded-badge px-1 text-xs text-danger-strong hover:underline"
                    onClick={() => onDelete(session)}
                  >
                    delete
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
        <Pager
          pager={pager}
          shown={sessions.length}
          total={total}
          nextCursor={nextCursor}
          label="sessions"
        />
      </aside>
    </>
  );
}
