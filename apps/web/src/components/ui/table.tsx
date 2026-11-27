"use client";

import type { ReactNode } from "react";

import type { Table } from "@/lib/useTable";

/** Show a table's search box once it has more rows than fit comfortably on screen. */
export const SEARCH_THRESHOLD = 10;

/**
 * A column header that sorts its table. The whole header is the button, and
 * `aria-sort` tells assistive technology which column is sorted and which way.
 */
export function SortHeader<Key extends string>({
  table,
  column,
  numeric = false,
  children,
}: {
  table: Pick<Table<unknown, Key>, "sort" | "sortBy">;
  column: NoInfer<Key>;
  numeric?: boolean;
  children: ReactNode;
}) {
  const sorted = table.sort?.key === column ? table.sort.direction : null;
  return (
    <th
      scope="col"
      className={numeric ? "numeric" : undefined}
      aria-sort={sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : "none"}
    >
      <SortButton table={table} column={column} numeric={numeric}>
        {children}
      </SortButton>
    </th>
  );
}

/**
 * The button inside a sorting header. On its own where one column shows two
 * things, each sortable: "Status · Last sign-in" is two of these in one header.
 */
export function SortButton<Key extends string>({
  table,
  column,
  numeric = false,
  children,
}: {
  table: Pick<Table<unknown, Key>, "sort" | "sortBy">;
  column: NoInfer<Key>;
  numeric?: boolean;
  children: ReactNode;
}) {
  const sorted = table.sort?.key === column ? table.sort.direction : null;
  return (
    <button
      type="button"
      onClick={() => table.sortBy(column)}
      className={`inline-flex items-center gap-1 font-[inherit] uppercase tracking-[inherit] hover:text-heading ${
        numeric ? "flex-row-reverse" : ""
      } ${sorted ? "text-heading" : ""}`}
    >
      {children}
      <span aria-hidden="true" className={sorted ? "" : "opacity-30"}>
        {sorted === "desc" ? "▼" : "▲"}
      </span>
    </button>
  );
}

/**
 * A table's search box and row count. It appears once the table has more than
 * SEARCH_THRESHOLD rows (or while a search is typed), so a short table stays plain
 * and a long one, such as the audit log, can be narrowed to what you need.
 */
export function TableSearch({
  table,
  label,
  placeholder = "Filter rows",
}: {
  table: Pick<Table<unknown, string>, "searchable" | "total" | "query" | "setQuery" | "rows">;
  /** What the rows are, for the box's accessible name ("Search audit events"). */
  label: string;
  placeholder?: string;
}) {
  if (!table.searchable || (table.total <= SEARCH_THRESHOLD && !table.query)) return null;
  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-border px-5 py-2.5">
      <input
        type="search"
        aria-label={`Search ${label}`}
        placeholder={placeholder}
        value={table.query}
        onChange={(event) => table.setQuery(event.target.value)}
        className="w-full max-w-xs rounded-control border border-border-strong bg-surface px-3 py-1.5 text-sm text-ink shadow-control placeholder:text-muted focus:border-primary"
      />
      <span className="text-xs text-muted" aria-live="polite">
        {table.query ? `${table.rows.length} of ${table.total}` : `${table.total} rows`}
      </span>
    </div>
  );
}
