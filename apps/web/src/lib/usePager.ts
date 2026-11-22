"use client";

import { useCallback, useState } from "react";

/** Rows a long list shows at a time. */
export const PAGE_SIZE = 50;

export interface Pager {
  /** The cursor for the page on screen: null for the first page. */
  cursor: string | null;
  /** Zero-based page number, for "51–100 of 3,420". */
  page: number;
  pageSize: number;
  next: (nextCursor: string) => void;
  previous: () => void;
  /** Back to the first page, as a change of filter must. */
  reset: () => void;
}

/**
 * Where a keyset-paged list is. The API answers one page and the cursor to the
 * next, so going back means remembering every cursor that led here; a browser
 * never holds more than one page of a list that can run to tens of thousands.
 */
export function usePager(pageSize: number = PAGE_SIZE): Pager {
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const next = useCallback((nextCursor: string) => setCursors((c) => [...c, nextCursor]), []);
  const previous = useCallback(
    () => setCursors((c) => (c.length > 1 ? c.slice(0, -1) : c)),
    [],
  );
  const reset = useCallback(() => setCursors([null]), []);
  return {
    cursor: cursors[cursors.length - 1] ?? null,
    page: cursors.length - 1,
    pageSize,
    next,
    previous,
    reset,
  };
}
