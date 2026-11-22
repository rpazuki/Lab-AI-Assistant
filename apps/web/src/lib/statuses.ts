/**
 * What each status the app shows means, in one place.
 *
 * `statuses.json` is the single glossary: the info button beside a state chip and
 * the status tables in the how-to guides both read it, so a chip and its guide can
 * never disagree. Each group names the backend enum it describes, and
 * `tests/contract/test_status_glossary.py` fails when the enum gains a value the
 * glossary does not explain, or the glossary explains one the enum no longer has.
 */

import glossary from "@/lib/statuses.json";

export type StatusKind = keyof typeof glossary;

interface StatusGroup {
  title: string;
  /** The backend enum, as a dotted import path; absent for display-only sets. */
  enum?: string;
  /** Values the page shows that the enum does not have (for example `pending`). */
  ui_only?: string[];
  values: Record<string, string>;
}

const GROUPS = glossary as Record<StatusKind, StatusGroup>;

export const STATUS_KINDS = Object.keys(GROUPS) as StatusKind[];

export function statusGroup(kind: StatusKind): StatusGroup {
  return GROUPS[kind];
}

/** The short description of a value, or null when the glossary does not have it. */
export function describeStatus(kind: StatusKind, value: string): string | null {
  return GROUPS[kind].values[value] ?? null;
}
