"use client";

import { Button } from "@/components/ui/primitives";
import type { Pager as PagerState } from "@/lib/usePager";

/** "51–100 of 3,420 candidates", or "3,420 candidates" when it is all one page. */
function describe(pager: PagerState, shown: number, total: number, label: string): string {
  if (pager.page === 0 && shown >= total) return `${total.toLocaleString()} ${label}`;
  if (shown === 0) return `none of ${total.toLocaleString()} ${label} on this page`;
  const first = pager.page * pager.pageSize + 1;
  const last = pager.page * pager.pageSize + shown;
  return `${first.toLocaleString()}–${last.toLocaleString()} of ${total.toLocaleString()} ${label}`;
}

/**
 * The count of a paged list, for the top of it: the same words the pager says at
 * the bottom, so a long list says how long it is before you scroll.
 */
export function PageCount({
  pager,
  shown,
  total,
  label,
  className = "",
}: {
  pager: PagerState;
  shown: number;
  total: number;
  label: string;
  className?: string;
}) {
  return (
    <span className={`text-xs text-muted ${className}`} data-testid={`count-${label}`}>
      {describe(pager, shown, total, label)}
    </span>
  );
}

/**
 * "51–100 of 3,420", with Previous and Next. Shown under any list the API pages,
 * so a long list is read a page at a time and the total is always in view. With
 * `sizes`, a menu beside the buttons sets how many rows a page shows.
 */
export function Pager({
  pager,
  shown,
  total,
  nextCursor,
  label,
  sizes,
}: {
  pager: PagerState;
  /** Rows on this page. */
  shown: number;
  /** Rows that match, across every page. */
  total: number;
  nextCursor: string | null;
  /** What the rows are, for the buttons' accessible names ("candidates"). */
  label: string;
  /** Page sizes to offer; the menu is left out without them. */
  sizes?: readonly number[];
}) {
  const single = pager.page === 0 && nextCursor === null;
  const menu = sizes ? (
    <label className="flex items-center gap-1">
      <span>Rows</span>
      <select
        aria-label={`Rows per page of ${label}`}
        className="rounded-control border border-border-strong bg-surface px-1.5 py-1 text-xs text-ink"
        value={pager.pageSize}
        onChange={(event) => pager.setPageSize(Number(event.target.value))}
      >
        {sizes.map((size) => (
          <option key={size} value={size}>
            {size}
          </option>
        ))}
      </select>
    </label>
  ) : null;
  if (single && !menu) {
    return total > 0 ? (
      <p className="border-t border-border px-5 py-2.5 text-xs text-muted">
        {total.toLocaleString()} {label}
      </p>
    ) : null;
  }
  return (
    <div className="flex flex-wrap items-center gap-3 border-t border-border px-5 py-2.5 text-xs text-muted">
      <span aria-live="polite">{describe(pager, shown, total, label)}</span>
      <span className="ml-auto flex items-center gap-2">
        {menu}
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
