"use client";

import { useCallback, useEffect, useState } from "react";

import { filterSelectClass, PaperFilterFields } from "@/components/PaperFilterFields";
import { LiveStatus } from "@/components/ui/LiveStatus";
import { CollapsiblePanel } from "@/components/ui/CollapsiblePanel";
import { PageCount, Pager } from "@/components/ui/Pager";
import { PanelSection } from "@/components/ui/PanelSection";
import { Alert, Button, Empty, StateChip, row, rowList } from "@/components/ui/primitives";
import { bulkRelevance, candidateFacets, listCandidates, setRelevance } from "@/lib/api";
import { languageName } from "@/lib/languages";
import { statusGroup } from "@/lib/statuses";
import { useLiveRefresh } from "@/lib/useLiveRefresh";
import { PAGE_SIZES, usePager } from "@/lib/usePager";
import type { Candidate, CandidateFacets, CandidateFilters, Page } from "@/types";

const EMPTY_PAGE: Page<Candidate> = { items: [], next_cursor: null, total: 0 };

/** The decisions a filter can apply to everything it matches, as the buttons say them. */
const BULK: { relevance: string; label: string; verb: string }[] = [
  { relevance: "included", label: "Include", verb: "Include" },
  { relevance: "included_uncertain", label: "Mark uncertain", verb: "Mark as uncertain" },
  { relevance: "excluded", label: "Exclude", verb: "Exclude" },
];

function active(filters: CandidateFilters): boolean {
  return Object.values(filters).some((value) => value !== undefined && value !== null && value !== "");
}

/**
 * Candidates, reviewed by rule.
 *
 * The filters narrow the list on the server, and the same filters are what a bulk
 * decision applies to, so what the list shows is exactly what "Exclude all" would
 * change. A bulk decision always previews first (how many, and a few of them) and
 * by default leaves alone any candidate someone has already decided.
 */
export function CandidateReview({ live: parentLive }: { live: boolean }) {
  const [page, setPage] = useState<Page<Candidate>>(EMPTY_PAGE);
  const [facets, setFacets] = useState<CandidateFacets | null>(null);
  const [draft, setDraft] = useState<CandidateFilters>({});
  const [filters, setFilters] = useState<CandidateFilters>({});
  const [onlyUnreviewed, setOnlyUnreviewed] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const pager = usePager();

  const refresh = useCallback(async () => {
    try {
      const [next, nextFacets] = await Promise.all([
        listCandidates({ after: pager.cursor, limit: pager.pageSize, ...filters }),
        candidateFacets(),
      ]);
      setPage(next);
      setFacets(nextFacets);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "candidates could not be loaded");
    }
  }, [pager.cursor, pager.pageSize, filters]);

  const live = useLiveRefresh(refresh, { active: parentLive });
  const reload = live.refresh;
  useEffect(() => {
    void reload();
  }, [refresh, reload]);

  function apply(next: CandidateFilters) {
    setFilters(next);
    pager.reset();
  }

  /** A choice from a list applies at once; typed text waits for Enter or Apply. */
  function choose<K extends keyof CandidateFilters>(key: K, value: CandidateFilters[K]) {
    const next = { ...draft, [key]: value };
    setDraft(next);
    apply(next);
  }

  async function decideAll(choice: (typeof BULK)[number]) {
    setMessage(null);
    setError(null);
    try {
      const body = { relevance: choice.relevance, only_unreviewed: onlyUnreviewed };
      const preview = await bulkRelevance(filters, { ...body, apply: false });
      if (preview.matched === 0) {
        setMessage(
          onlyUnreviewed
            ? "Nothing to change: every matching candidate has been decided already."
            : "Nothing to change.",
        );
        return;
      }
      const examples = preview.sample
        .slice(0, 3)
        .map((paper) => `• ${paper.title ?? "Untitled"}${paper.year ? ` (${paper.year})` : ""}`)
        .join("\n");
      const scope = onlyUnreviewed ? " not yet reviewed" : "";
      if (
        !window.confirm(
          `${choice.verb} ${preview.matched.toLocaleString()} candidate${preview.matched === 1 ? "" : "s"}${scope} matching these filters?\n\n${examples}\n\nEach is recorded with the filter that decided it.`,
        )
      ) {
        return;
      }
      let changed = 0;
      let remaining = preview.matched;
      setBusy(`${choice.verb}: 0 of ${preview.matched.toLocaleString()}`);
      // The server changes a bounded number per call and says how many remain.
      // Every round extends the history step the first one opened.
      let stepId: string | null = null;
      for (let round = 0; remaining > 0 && round < 200; round++) {
        const step = await bulkRelevance(filters, { ...body, apply: true, step_id: stepId });
        stepId = step.step_id ?? stepId;
        changed += step.applied;
        remaining = step.remaining;
        setBusy(`${choice.verb}: ${changed.toLocaleString()} of ${preview.matched.toLocaleString()}`);
        if (step.applied === 0) break;
      }
      setMessage(`${changed.toLocaleString()} candidate${changed === 1 ? "" : "s"} set to ${choice.relevance}.`);
      pager.reset();
      live.kick();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "the decision could not be applied");
    } finally {
      setBusy(null);
    }
  }

  const incomplete = facets !== null && facets.summarised < facets.works;

  return (
    <CollapsiblePanel
      title="Candidates"
      storageKey="collections.candidates"
      flush
      count={<PageCount pager={pager} shown={page.items.length} total={page.total} label="candidates" />}
      aside={<LiveStatus {...live} />}
    >
      <PanelSection title="Search" hint="Narrows the candidates below. The bulk action acts on exactly what it finds.">
      <form
        className="space-y-3"
        onSubmit={(event) => {
          event.preventDefault();
          apply(draft);
        }}
      >
        <PaperFilterFields
          draft={draft}
          facets={facets}
          onType={(patch) => setDraft((current) => ({ ...current, ...patch }))}
          onChoose={(patch) => {
            const next = { ...draft, ...patch };
            setDraft(next);
            apply(next);
          }}
          actions={
            <>
              <Button type="submit" size="sm">
                Apply filters
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={!active(draft) && !active(filters)}
                onClick={() => {
                  setDraft({});
                  apply({});
                }}
              >
                Clear
              </Button>
            </>
          }
        >
          <label className="space-y-1">
            <span className="block">Relevance</span>
            <select
              aria-label="Filter by relevance"
              className={filterSelectClass}
              value={draft.relevance ?? ""}
              onChange={(event) => choose("relevance", event.target.value)}
            >
              <option value="">All</option>
              {Object.keys(statusGroup("relevance").values).map((state) => (
                <option key={state} value={state}>
                  {state}
                </option>
              ))}
            </select>
          </label>
          <label className="space-y-1">
            <span className="block">Identity</span>
            <select
              aria-label="Filter by identity"
              className={filterSelectClass}
              value={draft.identity_status ?? ""}
              onChange={(event) => choose("identity_status", event.target.value)}
            >
              <option value="">All but merged</option>
              {Object.keys(statusGroup("identity").values).map((state) => (
                <option key={state} value={state}>
                  {state}
                </option>
              ))}
            </select>
          </label>
        </PaperFilterFields>
      </form>
      </PanelSection>

      <PanelSection title="Bulk action" hint="Decide every candidate the search found at once.">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="text-ink">
          {page.total.toLocaleString()} match{page.total === 1 ? "es" : ""}
          {active(filters) ? " these filters" : ""}. With all of them:
        </span>
        {BULK.map((choice) => (
          <Button
            key={choice.relevance}
            variant={choice.relevance === "excluded" ? "danger" : "ghost"}
            size="sm"
            disabled={busy !== null || page.total === 0}
            onClick={() => void decideAll(choice)}
          >
            {choice.label} all
          </Button>
        ))}
        <label className="ml-auto flex items-center gap-1.5 text-xs text-muted">
          <input
            type="checkbox"
            checked={onlyUnreviewed}
            onChange={(event) => setOnlyUnreviewed(event.target.checked)}
          />
          Leave candidates already decided as they are
        </label>
      </div>
      </PanelSection>

      {busy ? (
        <p className="px-5 py-2 text-sm text-muted" aria-live="polite">
          {busy}…
        </p>
      ) : null}
      {message ? (
        <div className="px-5 pt-3">
          <Alert tone="success">{message}</Alert>
        </div>
      ) : null}
      {error ? (
        <div className="px-5 pt-3">
          <Alert tone="danger" role="alert">
            {error}
          </Alert>
        </div>
      ) : null}
      {incomplete ? (
        <div className="px-5 pt-3">
          <Alert tone="warning">
            The filter index covers {facets.summarised.toLocaleString()} of {facets.works.toLocaleString()}{" "}
            papers. Filters on title, year and the like see only those until{" "}
            <code>lab db backfill-review</code> has run.
          </Alert>
        </div>
      ) : null}

      {page.items.length === 0 ? (
        <div className="px-5 py-3">
          <Empty>
            {active(filters) ? "No candidates match these filters." : "No candidates yet. Run discovery first."}
          </Empty>
        </div>
      ) : null}
      <ul className={`${rowList} text-sm`}>
        {page.items.map((candidate) => (
          <li key={candidate.work_id} className={row}>
            <div className="flex flex-wrap items-center gap-2">
              <strong className="text-heading">{candidate.title ?? "Untitled"}</strong>
              <StateChip state={candidate.relevance} kind="relevance" />
              <StateChip state={candidate.identity_status} kind="identity" />
              {candidate.retraction_status !== "none" ? (
                <StateChip state={candidate.retraction_status} kind="retraction" />
              ) : null}
            </div>
            <p className="mt-1 text-xs text-muted">
              {[
                candidate.journal,
                candidate.year,
                candidate.work_kind,
                candidate.language
                  ? `${languageName(candidate.language)}${candidate.language_detected ? " (read from the title)" : ""}`
                  : null,
              ]
                .filter(Boolean)
                .join(" · ")}
              {candidate.merged_into_work_id
                ? ` · merged into ${candidate.merged_into_work_id.slice(0, 8)}, which holds its records`
                : ""}
            </p>
            <p className="mt-1 font-mono text-xs text-neutral-strong">
              {candidate.identifiers.map((id) => `${id.scheme}:${id.value}`).join("  ")}
            </p>
            <details className="mt-2 text-xs">
              <summary className="cursor-pointer text-link hover:underline">
                which sources asserted these fields
              </summary>
              <ul className="mt-2 space-y-0.5 rounded-control bg-surface-subtle px-3 py-2">
                {Object.entries(candidate.provenance).map(([field, assertionIds]) => (
                  <li key={field}>
                    <span className="font-mono">{field}</span>: {assertionIds.length} assertion
                    {assertionIds.length === 1 ? "" : "s"}
                  </li>
                ))}
              </ul>
            </details>
            <div className="mt-3 flex gap-2">
              <Button
                variant="ghost"
                size="sm"
                onClick={async () => {
                  await setRelevance(candidate.work_id, "included", "steward_included");
                  live.kick();
                }}
              >
                Include
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={async () => {
                  await setRelevance(candidate.work_id, "excluded", "out_of_scope");
                  live.kick();
                }}
              >
                Exclude
              </Button>
            </div>
          </li>
        ))}
      </ul>
      <Pager
        pager={pager}
        shown={page.items.length}
        total={page.total}
        nextCursor={page.next_cursor}
        label="candidates"
        sizes={PAGE_SIZES}
      />
    </CollapsiblePanel>
  );
}
