"use client";

import { useState } from "react";

/** A value a column sorts by. Dates sort by time; null and undefined sort last. */
export type SortValue = string | number | boolean | Date | null | undefined;

export type SortDirection = "asc" | "desc";

export interface TableOptions<Row, Key extends string> {
  /** How to read each sortable column's value from a row. */
  columns: Record<Key, (row: Row) => SortValue>;
  /**
   * The order before anyone clicks a header. A table of events or anything timed
   * starts on its time column, newest first, so the latest is always on top.
   */
  defaultSort?: { key: NoInfer<Key>; direction: SortDirection };
  /** The text a search matches against; omit for a table that is never searched. */
  searchText?: (row: Row) => string;
}

export interface Table<Row, Key extends string> {
  /** The rows to draw: filtered, then sorted. */
  rows: Row[];
  /** Rows before filtering, so a page can say "12 of 340". */
  total: number;
  sort: { key: Key; direction: SortDirection } | null;
  /** Sort by a column; selecting the sorted column again reverses it. */
  sortBy: (key: Key) => void;
  query: string;
  setQuery: (query: string) => void;
  searchable: boolean;
}

/** An ISO timestamp, as the API sends them: compared as a time, not as text. */
const ISO_DATE = /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$/;

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

function comparable(value: SortValue): number | string | null {
  if (value === null || value === undefined || value === "") return null;
  if (value instanceof Date) return value.getTime();
  if (typeof value === "boolean") return value ? 1 : 0;
  if (typeof value === "string" && ISO_DATE.test(value)) {
    const time = Date.parse(value);
    return Number.isNaN(time) ? value : time;
  }
  // A measured value as text ("0.42 g/g", "-3 °C") sorts by its number, so 0.42
  // comes before 0.5; plain text and identifiers that merely start with a digit
  // compare as text.
  if (typeof value === "string" && LEADING_NUMBER.test(value)) {
    return Number.parseFloat(value);
  }
  return value;
}

/** A number, then nothing or a space and a unit: "12", "0.42 g/g", "-3.5 °C". */
const LEADING_NUMBER = /^[-+]?\d+(\.\d+)?([eE][-+]?\d+)?(\s.*)?$/;

/** Compare two column values in ascending order; empty values always go last. */
export function compareValues(left: SortValue, right: SortValue): number {
  const a = comparable(left);
  const b = comparable(right);
  if (a === null && b === null) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  return collator.compare(String(a), String(b));
}

/**
 * The one way a table sorts and filters.
 *
 * Every table in the app reads its rows through this, so each one sorts the same
 * way (a header click sorts, a second click reverses, empty values stay at the
 * bottom either way) and searches the same way (every word must appear,
 * case-insensitively). The sort is stable: rows that compare equal keep the order
 * the API sent them in.
 */
export function useTable<Row, Key extends string>(
  source: readonly Row[],
  { columns, defaultSort, searchText }: TableOptions<Row, Key>,
): Table<Row, Key> {
  const [sort, setSort] = useState<{ key: Key; direction: SortDirection } | null>(
    defaultSort ?? null,
  );
  const [query, setQuery] = useState("");

  // Recomputed every render: a few hundred rows sort in well under a millisecond,
  // and a memo keyed on `source` would keep stale `columns` closures.
  const rows = (() => {
    const words = query.toLowerCase().split(/\s+/).filter(Boolean);
    const filtered =
      searchText && words.length > 0
        ? source.filter((row) => {
            const text = searchText(row).toLowerCase();
            return words.every((word) => text.includes(word));
          })
        : [...source];
    if (!sort) return filtered;
    const read = columns[sort.key];
    const sign = sort.direction === "asc" ? 1 : -1;
    return filtered
      .map((row, index) => ({ row, index, value: read(row) }))
      .sort((left, right) => {
        const empty = comparable(left.value) === null || comparable(right.value) === null;
        // Empty values stay last in both directions: reversing must not float them up.
        const order = empty
          ? compareValues(left.value, right.value)
          : sign * compareValues(left.value, right.value);
        return order || left.index - right.index;
      })
      .map((entry) => entry.row);
  })();

  return {
    rows,
    total: source.length,
    sort,
    sortBy: (key) =>
      setSort((current) =>
        current?.key === key
          ? { key, direction: current.direction === "asc" ? "desc" : "asc" }
          : // A column first sorts ascending, except the default column, which
            // returns to its own direction (newest first for a time column).
            { key, direction: defaultSort?.key === key ? defaultSort.direction : "asc" },
      ),
    query,
    setQuery,
    searchable: Boolean(searchText),
  };
}
