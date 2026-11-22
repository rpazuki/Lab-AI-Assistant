"use client";

import { Button } from "@/components/ui/primitives";
import type { Pager as PagerState } from "@/lib/usePager";

/**
 * "51–100 of 3,420", with Previous and Next. Shown under any list the API pages,
 * so a long list is read a page at a time and the total is always in view.
 */
export function Pager({
  pager,
  shown,
  total,
  nextCursor,
  label,
}: {
  pager: PagerState;
  /** Rows on this page. */
  shown: number;
  /** Rows that match, across every page. */
  total: number;
  nextCursor: string | null;
  /** What the rows are, for the buttons' accessible names ("candidates"). */
  label: string;
}) {
  if (pager.page === 0 && nextCursor === null) {
    return total > 0 ? (
      <p className="border-t border-border px-5 py-2.5 text-xs text-muted">
        {total.toLocaleString()} {label}
      </p>
    ) : null;
  }
  const first = pager.page * pager.pageSize + 1;
  const last = pager.page * pager.pageSize + shown;
  return (
    <div className="flex flex-wrap items-center gap-3 border-t border-border px-5 py-2.5 text-xs text-muted">
      <span aria-live="polite">
        {shown === 0
          ? `none of ${total.toLocaleString()} ${label} on this page`
          : `${first.toLocaleString()}–${last.toLocaleString()} of ${total.toLocaleString()} ${label}`}
      </span>
      <span className="ml-auto flex gap-2">
        <Button
          variant="ghost"
          size="sm"
          aria-label={`Previous page of ${label}`}
          disabled={pager.page === 0}
          onClick={pager.previous}
        >
          Previous
        </Button>
        <Button
          variant="ghost"
          size="sm"
          aria-label={`Next page of ${label}`}
          disabled={nextCursor === null}
          onClick={() => {
            if (nextCursor) pager.next(nextCursor);
          }}
        >
          Next
        </Button>
      </span>
    </div>
  );
}
