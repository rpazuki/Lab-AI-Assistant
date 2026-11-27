"use client";

/**
 * What a paper must be like: the search fields the candidate list and the
 * acquisition queue share, so the two boxes find papers the same way.
 *
 * Typed text waits for Enter or the form's apply button (`onType`); a choice from
 * a list applies at once (`onChoose`). Each box adds its own selects after these
 * (`children`) and its buttons last (`actions`): relevance and identity for
 * candidates, status and last answer for the queue.
 */

import type { ReactNode } from "react";

import { Input } from "@/components/ui/primitives";
import { languageName } from "@/lib/languages";
import type { CandidateFacets, PaperFilters } from "@/types";

export const filterSelectClass =
  "rounded-control border border-border-strong bg-surface px-2 py-1.5 text-sm text-ink";

/** The language menu's value for a filter: "is:es", "not:en,unknown", or "" for any. */
function languageChoice(filters: PaperFilters): string {
  if (filters.language) return `is:${filters.language}`;
  if (filters.exclude_language) return `not:${filters.exclude_language}`;
  return "";
}

function fromLanguageChoice(choice: string): Pick<PaperFilters, "language" | "exclude_language"> {
  if (choice.startsWith("is:")) return { language: choice.slice(3), exclude_language: undefined };
  if (choice.startsWith("not:")) return { language: undefined, exclude_language: choice.slice(4) };
  return { language: undefined, exclude_language: undefined };
}

export function PaperFilterFields({
  draft,
  facets,
  onType,
  onChoose,
  leading,
  children,
  actions,
}: {
  draft: PaperFilters;
  facets: CandidateFacets | null;
  /** Typed text: changes what is on screen, applied with the form. */
  onType: (patch: Partial<PaperFilters>) => void;
  /** A choice from a list: applied at once. */
  onChoose: (patch: Partial<PaperFilters>) => void;
  /** Fields of the box's own before the shared text fields. */
  leading?: ReactNode;
  /** The box's own selects, after the shared ones. */
  children?: ReactNode;
  /** The box's buttons, at the end of the row. */
  actions: ReactNode;
}) {
  return (
    <>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {leading}
        <label className="space-y-1 text-xs text-muted">
          <span className="block">Title has all of</span>
          <Input
            aria-label="Title has all of"
            placeholder="e.g., yarrowia, lipid"
            value={draft.title ?? ""}
            onChange={(event) => onType({ title: event.target.value })}
          />
        </label>
        <label className="space-y-1 text-xs text-muted">
          <span className="block">Title has none of</span>
          <Input
            aria-label="Title has none of"
            placeholder="e.g., review, erratum"
            value={draft.exclude ?? ""}
            onChange={(event) => onType({ exclude: event.target.value })}
          />
        </label>
        <label className="space-y-1 text-xs text-muted">
          <span className="block">Subject term, any of</span>
          <Input
            aria-label="Subject term, any of"
            placeholder="e.g., yeasts, lipid metabolism"
            value={draft.subject ?? ""}
            onChange={(event) => onType({ subject: event.target.value })}
          />
        </label>
        <label className="flex items-center gap-2 text-xs text-ink sm:col-span-2 lg:col-span-4">
          <input
            type="checkbox"
            checked={Boolean(draft.in_abstract)}
            onChange={(event) => onChoose({ in_abstract: event.target.checked })}
          />
          <span>Look for the words in the abstract and subject terms too, not the title alone</span>
        </label>
        <label className="space-y-1 text-xs text-muted">
          <span className="block">Journal contains</span>
          <Input
            aria-label="Journal contains"
            value={draft.journal ?? ""}
            onChange={(event) => onType({ journal: event.target.value })}
          />
        </label>
      </div>
      <div className="flex flex-wrap items-end gap-3 text-xs text-muted">
        <label className="space-y-1">
          <span className="block">Year from</span>
          <Input
            aria-label="Year from"
            type="number"
            className="w-24"
            placeholder={facets?.year_min ? String(facets.year_min) : ""}
            value={draft.year_from ?? ""}
            onChange={(event) =>
              onType({ year_from: event.target.value ? Number(event.target.value) : null })
            }
          />
        </label>
        <label className="space-y-1">
          <span className="block">to</span>
          <Input
            aria-label="Year to"
            type="number"
            className="w-24"
            placeholder={facets?.year_max ? String(facets.year_max) : ""}
            value={draft.year_to ?? ""}
            onChange={(event) =>
              onType({ year_to: event.target.value ? Number(event.target.value) : null })
            }
          />
        </label>
        <label className="space-y-1">
          <span className="block">Kind</span>
          <select
            aria-label="Filter by kind"
            className={filterSelectClass}
            value={draft.kind ?? ""}
            onChange={(event) => onChoose({ kind: event.target.value })}
          >
            <option value="">All</option>
            {Object.entries(facets?.kinds ?? {})
              .filter(([kind]) => kind)
              .map(([kind, count]) => (
                <option key={kind} value={kind}>
                  {kind} ({count.toLocaleString()})
                </option>
              ))}
          </select>
        </label>
        <label className="space-y-1">
          <span className="block">Found by</span>
          <select
            aria-label="Filter by source"
            className={filterSelectClass}
            value={draft.source ?? ""}
            onChange={(event) => onChoose({ source: event.target.value })}
          >
            <option value="">Any source</option>
            {Object.entries(facets?.sources ?? {}).map(([source, count]) => (
              <option key={source} value={source}>
                {source} ({count.toLocaleString()})
              </option>
            ))}
          </select>
        </label>
        <label className="space-y-1">
          <span className="block">Language</span>
          <select
            aria-label="Filter by language"
            className={filterSelectClass}
            value={languageChoice(draft)}
            onChange={(event) => onChoose(fromLanguageChoice(event.target.value))}
          >
            <option value="">Any language</option>
            <option value="not:en,unknown">Not English (language known)</option>
            <option value="not:en">Not English, or unknown</option>
            {Object.entries(facets?.languages ?? {}).map(([code, count]) => (
              <option key={code} value={`is:${code}`}>
                {languageName(code)} ({count.toLocaleString()})
              </option>
            ))}
          </select>
        </label>
        <label className="space-y-1">
          <span className="block">Abstract</span>
          <select
            aria-label="Filter by abstract"
            className={filterSelectClass}
            value={
              draft.has_abstract === null || draft.has_abstract === undefined
                ? ""
                : String(draft.has_abstract)
            }
            onChange={(event) =>
              onChoose({
                has_abstract: event.target.value === "" ? null : event.target.value === "true",
              })
            }
          >
            <option value="">Either</option>
            <option value="true">Has one</option>
            <option value="false">Has none</option>
          </select>
        </label>
        {children}
        <span className="ml-auto flex gap-2">{actions}</span>
      </div>
    </>
  );
}
