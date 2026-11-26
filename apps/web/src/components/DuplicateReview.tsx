"use client";

import { useCallback, useEffect, useState } from "react";

import { LiveStatus } from "@/components/ui/LiveStatus";
import { CollapsiblePanel } from "@/components/ui/CollapsiblePanel";
import { PageCount, Pager } from "@/components/ui/Pager";
import { PanelSection } from "@/components/ui/PanelSection";
import { Alert, Button, Empty, Input } from "@/components/ui/primitives";
import {
  applyMergeRule,
  dismissDuplicate,
  keepConflictSeparate,
  keepConflictsSeparate,
  mergeConflictsSamePaper,
  listConflicts,
  listIdentityConflicts,
  mergeDuplicate,
  mergeWorks,
  previewMergeRule,
  type PairSearch,
} from "@/lib/api";
import { useLiveRefresh } from "@/lib/useLiveRefresh";
import { PAGE_SIZES, usePager } from "@/lib/usePager";
import type { DuplicateProposal, IdentityConflictPage, MergePlan, MergeRule, Page, PaperSummary } from "@/types";

/** The cautious rule: exact titles, same year, no contradicting authors or DOIs. */
export const DEFAULT_RULE: MergeRule = {
  min_similarity: 1,
  require_same_year: true,
  skip_different_authors: true,
  require_known_authors: false,
  skip_different_dois: true,
  max_pairs_per_work: 3,
  max_group_size: 3,
};

const EMPTY: Page<DuplicateProposal> = { items: [], next_cursor: null, total: 0 };

function Paper({ paper }: { paper: PaperSummary }) {
  return (
    <div className="min-w-0 flex-1 rounded-control border border-border bg-surface px-3 py-2">
      <p className="font-medium text-heading">{paper.title ?? "Untitled"}</p>
      <p className="mt-0.5 text-xs text-muted">
        {[paper.first_author, paper.year, paper.journal, paper.resource_kind].filter(Boolean).join(" · ")}
      </p>
      {paper.dois ? (
        <p className="mt-0.5 text-xs text-muted">
          {paper.dois.length === 0
            ? "No DOI"
            : paper.dois.map((doi, index) => (
                <span key={doi}>
                  {index === 0 ? "DOI " : ", "}
                  <a
                    className="font-mono text-link underline"
                    href={`https://doi.org/${doi}`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {doi}
                  </a>
                </span>
              ))}
        </p>
      ) : null}
      {paper.sources.length > 0 ? (
        <p className="mt-0.5 text-xs text-muted">found by {paper.sources.join(", ")}</p>
      ) : null}
    </div>
  );
}

function Check({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="flex items-center gap-1.5 text-sm text-ink">
      <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} />
      {label}
    </label>
  );
}

/**
 * Duplicate review: a merge rule for the many, and the queue for the rest.
 *
 * The rule is previewed before anything merges: how many papers it would merge,
 * and how many pairs each guard skips, with examples, so the threshold and
 * guards can be tuned against the real collection. The queue then holds what the
 * rule left, most similar first, each pair shown side by side with Merge and
 * "Not the same"; a pair either answer settles leaves the queue.
 */
export function DuplicateReview({
  live: parentLive,
  onChanged,
}: {
  live: boolean;
  onChanged?: () => void;
}) {
  const [view, setView] = useState<"pairs" | "conflicts">("pairs");
  const [pairs, setPairs] = useState<Page<DuplicateProposal>>(EMPTY);
  const [conflicts, setConflicts] = useState(0);
  // The search on screen, and the one the list and the rule were asked for.
  const [draft, setDraft] = useState<PairSearch>({});
  const [search, setSearch] = useState<PairSearch>({});
  const [rule, setRule] = useState<MergeRule>(DEFAULT_RULE);
  const [plan, setPlan] = useState<MergePlan | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const pager = usePager();

  const refresh = useCallback(async () => {
    try {
      const body = await listConflicts({
        after: pager.cursor,
        limit: pager.pageSize,
        ...search,
      });
      setPairs({ items: body.duplicate_proposals, next_cursor: body.next_cursor, total: body.proposal_total });
      setConflicts(body.conflict_total);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "the duplicate queue could not be loaded");
    }
  }, [pager.cursor, pager.pageSize, search]);

  const live = useLiveRefresh(refresh, { active: parentLive });
  const reload = live.refresh;
  useEffect(() => {
    void reload();
  }, [refresh, reload]);

  function change<K extends keyof MergeRule>(key: K, value: MergeRule[K]) {
    setRule((current) => ({ ...current, [key]: value }));
    // A preview answers the rule it was asked for, not the one now on screen.
    setPlan(null);
  }

  async function preview() {
    setError(null);
    setMessage(null);
    try {
      setBusy("Checking every open pair against the rule");
      setPlan(await previewMergeRule(rule, search));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "the rule could not be checked");
    } finally {
      setBusy(null);
    }
  }

  async function mergeAll() {
    if (!plan || plan.groups === 0) return;
    if (
      !window.confirm(
        `Merge ${plan.works_merged.toLocaleString()} paper${plan.works_merged === 1 ? "" : "s"} into ${plan.groups.toLocaleString()} (${plan.accepted_pairs.toLocaleString()} pairs)? Each merge keeps the paper with more identifiers and is recorded with this rule. A merge is not undone automatically.`,
      )
    ) {
      return;
    }
    setError(null);
    let merged = 0;
    try {
      let remaining = plan.groups;
      // Every round extends the history step the first one opened.
      let stepId: string | null = null;
      for (let round = 0; remaining > 0 && round < 500; round++) {
        setBusy(`Merging: ${merged.toLocaleString()} of ${plan.works_merged.toLocaleString()} papers`);
        const step = await applyMergeRule(rule, stepId, search);
        stepId = step.step_id ?? stepId;
        merged += step.works_merged;
        remaining = step.remaining_groups;
        if (step.groups_merged === 0) break;
      }
      setMessage(`${merged.toLocaleString()} paper${merged === 1 ? "" : "s"} merged.`);
      setPlan(null);
      pager.reset();
      live.kick();
      onChanged?.();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "the merge stopped part way; preview again to continue");
    } finally {
      setBusy(null);
    }
  }

  /** Merge the pairs skipped for one reason, with that check off and every other kept. */
  async function mergeAnyway(skip: MergePlan["skipped"][number]) {
    const overridden: MergeRule = { ...rule, override: skip.reason };
    setError(null);
    setMessage(null);
    let check: MergePlan;
    try {
      setBusy(`Checking the ${skip.count.toLocaleString()} skipped pairs again without that check`);
      check = await previewMergeRule(overridden, search);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "the skipped pairs could not be checked");
      return;
    } finally {
      setBusy(null);
    }
    const still = check.skipped.reduce((sum, other) => sum + other.count, 0);
    const stillWhy = check.skipped.map((other) => `${other.count.toLocaleString()}: ${other.description}`).join("; ");
    if (check.groups === 0) {
      setMessage(`None of these can be merged: each also fails another check (${stillWhy}).`);
      return;
    }
    if (
      !window.confirm(
        `Merge ${check.works_merged.toLocaleString()} paper${check.works_merged === 1 ? "" : "s"} into ` +
          `${check.groups.toLocaleString()} (${check.accepted_pairs.toLocaleString()} of the ` +
          `${skip.count.toLocaleString()} pairs skipped because ${skip.description})?` +
          (still > 0 ? `\n\n${still.toLocaleString()} stay skipped, for another reason (${stillWhy}).` : "") +
          "\n\nEach merge is recorded as your decision to override this check, and is not undone automatically.",
      )
    ) {
      return;
    }
    let merged = 0;
    try {
      let remaining = check.groups;
      let stepId: string | null = null;
      for (let round = 0; remaining > 0 && round < 500; round++) {
        setBusy(`Merging: ${merged.toLocaleString()} of ${check.works_merged.toLocaleString()} papers`);
        const step = await applyMergeRule(overridden, stepId, search);
        stepId = step.step_id ?? stepId;
        merged += step.works_merged;
        remaining = step.remaining_groups;
        if (step.groups_merged === 0) break;
      }
      setMessage(`${merged.toLocaleString()} paper${merged === 1 ? "" : "s"} merged, overriding: ${skip.description}.`);
      pager.reset();
      live.kick();
      onChanged?.();
      // The report under the rule is out of date: ask again what it skips now.
      setPlan(await previewMergeRule(rule, search));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "the merge stopped part way; press again to continue");
    } finally {
      setBusy(null);
    }
  }

  function find(next: PairSearch) {
    setSearch(next);
    // A preview answers the pairs it was asked about, not the ones now shown.
    setPlan(null);
    pager.reset();
  }

  async function decide(pair: DuplicateProposal, action: "merge" | "dismiss") {
    setError(null);
    try {
      if (action === "merge") await mergeDuplicate(pair.id);
      else await dismissDuplicate(pair.id);
      live.kick();
      onChanged?.();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "the decision could not be saved");
    }
  }

  return (
    <CollapsiblePanel
      title="Duplicates and identity conflicts"
      storageKey="collections.duplicates"
      flush
      aside={<LiveStatus {...live} />}
    >
      <div role="tablist" aria-label="Show" className="flex gap-1 border-b border-border px-5 pt-2">
        {(
          [
            ["pairs", `Pairs to review (${pairs.total.toLocaleString()})`],
            ["conflicts", `Identity conflicts (${conflicts.toLocaleString()})`],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={view === key}
            onClick={() => setView(key)}
            className={`-mb-px rounded-t-control border px-3 py-1.5 text-sm ${
              view === key
                ? "border-border border-b-surface bg-surface font-semibold text-heading"
                : "border-transparent text-muted hover:text-heading"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {view === "conflicts" ? (
        <ConflictReview onChanged={() => { live.kick(); onChanged?.(); }} />
      ) : (
      <>
      {conflicts > 0 ? (
        <div className="px-5 pt-3">
          <Alert tone="danger">
            {conflicts.toLocaleString()} paper{conflicts === 1 ? "" : "s"} hold contradictory strong
            identifiers and cannot enter a release until settled: see{" "}
            <button type="button" className="underline" onClick={() => setView("conflicts")}>
              Identity conflicts
            </button>
            .
          </Alert>
        </div>
      ) : null}
      <PanelSection title="Search" hint="Narrows the pairs below. Merge by rule acts on exactly what it finds.">
        <form
          className="flex flex-wrap items-end gap-x-5 gap-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            find(draft);
          }}
        >
          <label className="space-y-1 text-xs text-muted">
            <span className="block">Either title has all of</span>
            <Input
              aria-label="Either title has all of"
              placeholder="e.g., yarrowia, lipid"
              value={draft.q ?? ""}
              onChange={(event) => setDraft({ ...draft, q: event.target.value })}
            />
          </label>
          <label className="space-y-1 text-xs text-muted">
            <span className="block">Similarity at least</span>
            <select
              aria-label="Show pairs with similarity at least"
              className="rounded-control border border-border-strong bg-surface px-2 py-1.5 text-sm text-ink"
              value={draft.min_similarity ?? ""}
              onChange={(event) => {
                const next = { ...draft, min_similarity: event.target.value ? Number(event.target.value) : null };
                setDraft(next);
                find(next);
              }}
            >
              <option value="">any</option>
              {[0.9, 0.95, 1].map((value) => (
                <option key={value} value={value}>
                  {value.toFixed(2)}
                </option>
              ))}
            </select>
          </label>
          <label className="space-y-1 text-xs text-muted">
            <span className="block">Years</span>
            <select
              aria-label="Filter pairs by year"
              className="rounded-control border border-border-strong bg-surface px-2 py-1.5 text-sm text-ink"
              value={draft.year ?? ""}
              onChange={(event) => {
                const next = { ...draft, year: (event.target.value || null) as PairSearch["year"] };
                setDraft(next);
                find(next);
              }}
            >
              <option value="">Any</option>
              <option value="same">Same year</option>
              <option value="different">Different years</option>
              <option value="unknown">A year is missing</option>
            </select>
          </label>
          <label className="space-y-1 text-xs text-muted">
            <span className="block">First authors</span>
            <select
              aria-label="Filter pairs by first author"
              className="rounded-control border border-border-strong bg-surface px-2 py-1.5 text-sm text-ink"
              value={draft.authors ?? ""}
              onChange={(event) => {
                const next = { ...draft, authors: (event.target.value || null) as PairSearch["authors"] };
                setDraft(next);
                find(next);
              }}
            >
              <option value="">Any</option>
              <option value="agree">Agree</option>
              <option value="differ">Differ</option>
              <option value="unknown">An author is missing</option>
            </select>
          </label>
          <span className="flex gap-2">
            <Button type="submit" size="sm">
              Search
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => {
                setDraft({});
                find({});
              }}
            >
              Clear
            </Button>
          </span>
        </form>
      </PanelSection>

      <PanelSection title="Bulk action" hint="Merge by rule: of the pairs the search found, merge those the rule accepts.">
        <div className="flex flex-wrap items-end gap-x-5 gap-y-3">
          <label className="space-y-1 text-xs text-muted">
            <span className="block">Title similarity at least</span>
            <Input
              aria-label="Title similarity at least"
              type="number"
              min={0.85}
              max={1}
              step={0.01}
              className="w-24"
              value={rule.min_similarity}
              onChange={(event) => change("min_similarity", Number(event.target.value))}
            />
          </label>
          <label className="space-y-1 text-xs text-muted">
            <span className="block">Skip titles in more than</span>
            <span className="flex items-center gap-1.5">
              <Input
                aria-label="Skip titles in more than this many pairs"
                type="number"
                min={1}
                max={100}
                className="w-20"
                value={rule.max_pairs_per_work}
                onChange={(event) => change("max_pairs_per_work", Number(event.target.value))}
              />
              pairs
            </span>
          </label>
          <label className="space-y-1 text-xs text-muted">
            <span className="block">At most</span>
            <span className="flex items-center gap-1.5">
              <Input
                aria-label="Merge at most this many papers into one"
                type="number"
                min={2}
                max={20}
                className="w-20"
                value={rule.max_group_size}
                onChange={(event) => change("max_group_size", Number(event.target.value))}
              />
              papers into one
            </span>
          </label>
        </div>
        <div className="flex flex-wrap gap-x-5 gap-y-2">
          <Check
            label="Same year"
            checked={rule.require_same_year}
            onChange={(value) => change("require_same_year", value)}
          />
          <Check
            label="First authors do not differ"
            checked={rule.skip_different_authors}
            onChange={(value) => change("skip_different_authors", value)}
          />
          <Check
            label="Both first authors known"
            checked={rule.require_known_authors}
            onChange={(value) => change("require_known_authors", value)}
          />
          <Check
            label="Not two different DOIs"
            checked={rule.skip_different_dois}
            onChange={(value) => change("skip_different_dois", value)}
          />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="ghost" size="sm" disabled={busy !== null} onClick={() => void preview()}>
            Preview
          </Button>
          {plan ? (
            <Button size="sm" disabled={busy !== null || plan.groups === 0} onClick={() => void mergeAll()}>
              Merge {plan.works_merged.toLocaleString()} paper{plan.works_merged === 1 ? "" : "s"}
            </Button>
          ) : null}
          {busy ? (
            <span className="text-sm text-muted" aria-live="polite">
              {busy}…
            </span>
          ) : null}
        </div>
        {message ? <Alert tone="success">{message}</Alert> : null}
        {error ? (
          <Alert tone="danger" role="alert">
            {error}
          </Alert>
        ) : null}
        {plan ? (
          <div className="space-y-3 rounded-control border border-border bg-surface-subtle p-3 text-sm">
            <p className="text-ink">
              Of {plan.considered.toLocaleString()} open pairs, this rule merges{" "}
              <strong>{plan.accepted_pairs.toLocaleString()}</strong>: {plan.works_merged.toLocaleString()}{" "}
              papers into {plan.groups.toLocaleString()}.
            </p>
            {plan.skipped.length > 0 ? (
              <ul className="space-y-1">
                {plan.skipped.map((skip) => (
                  <li key={skip.reason}>
                    <details>
                      <summary className="cursor-pointer">
                        <span className="font-mono text-xs">{skip.count.toLocaleString()}</span> skipped:{" "}
                        {skip.description}
                      </summary>
                      {skip.overridable ? (
                        <div className="mt-2 flex flex-wrap items-center gap-2">
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={busy !== null}
                            onClick={() => void mergeAnyway(skip)}
                          >
                            Merge {skip.count === 1 ? "this pair" : `these ${skip.count.toLocaleString()} pairs`} anyway
                          </Button>
                          <span className="text-xs text-muted">
                            Checks them again without this check; every other check still applies, and you confirm
                            first.
                          </span>
                        </div>
                      ) : null}
                      <ul className="mt-2 space-y-2">
                        {skip.examples.map((example, index) => (
                          <li key={index} className="flex flex-col gap-2 sm:flex-row">
                            <Paper paper={example.left} />
                            <Paper paper={example.right} />
                          </li>
                        ))}
                      </ul>
                    </details>
                  </li>
                ))}
              </ul>
            ) : null}
            {plan.sample_groups.length > 0 ? (
              <details>
                <summary className="cursor-pointer">What it would merge, most similar first</summary>
                <ul className="mt-2 space-y-2">
                  {plan.sample_groups.map((group, index) => (
                    <li key={index} className="flex flex-col gap-2 sm:flex-row">
                      {group.papers.map((paper) => (
                        <Paper key={paper.work_id} paper={paper} />
                      ))}
                    </li>
                  ))}
                </ul>
              </details>
            ) : null}
          </div>
        ) : null}
      </PanelSection>

      <div className="flex flex-wrap items-center gap-3 border-b border-border px-5 py-2.5 text-sm">
        <h3 className="font-semibold text-heading">Pairs to review</h3>
        <PageCount pager={pager} shown={pairs.items.length} total={pairs.total} label="duplicate pairs" />
      </div>
      {pairs.items.length === 0 ? (
        <div className="px-5 py-3">
          <Empty>No duplicate pairs are waiting.</Empty>
        </div>
      ) : null}
      <ul className="divide-y divide-border">
        {pairs.items.map((pair) => (
          <li key={pair.id} className="space-y-2 px-5 py-3 text-sm">
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
              <span className="font-mono">similarity {pair.similarity.toFixed(2)}</span>
              {pair.year_gap !== null && pair.year_gap > 0 ? <span>{pair.year_gap} years apart</span> : null}
              {pair.first_author_match === false ? <span>different first authors</span> : null}
              <span className="ml-auto flex gap-2">
                <Button variant="ghost" size="sm" onClick={() => void decide(pair, "merge")}>
                  Merge
                </Button>
                <Button variant="ghost" size="sm" onClick={() => void decide(pair, "dismiss")}>
                  Not the same
                </Button>
              </span>
            </div>
            <div className="flex flex-col gap-2 sm:flex-row">
              <Paper paper={pair.left} />
              <Paper paper={pair.right} />
            </div>
          </li>
        ))}
      </ul>
      <Pager
        pager={pager}
        shown={pairs.items.length}
        total={pairs.total}
        nextCursor={pairs.next_cursor}
        label="duplicate pairs"
        sizes={PAGE_SIZES}
      />
      </>
      )}
    </CollapsiblePanel>
  );
}

const WHY: Record<string, string> = {
  two_papers_matched_one_record:
    "A source record named identifiers both papers hold, and they could not be merged automatically",
  identifier_held_by_another_paper: "A source gave this paper an identifier the other paper already holds",
};

const NO_CONFLICTS: IdentityConflictPage = { items: [], next_cursor: null, total: 0, pairs: 0 };

/**
 * Papers whose strong identifiers the sources disagree on. Each is shown with the
 * other paper involved and why, to settle as one paper (merge) or two (keep
 * separate). A conflicted paper cannot enter a release until settled.
 */
function ConflictReview({ onChanged }: { onChanged?: () => void }) {
  const [rows, setRows] = useState<IdentityConflictPage>(NO_CONFLICTS);
  // Until the first answer, an empty list means "not known yet", not "none".
  const [loaded, setLoaded] = useState(false);
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const pager = usePager();

  const refresh = useCallback(async () => {
    try {
      setRows(await listIdentityConflicts({ after: pager.cursor, limit: pager.pageSize, q: query || undefined }));
      setLoaded(true);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "the conflicts could not be loaded");
    }
  }, [pager.cursor, pager.pageSize, query]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function settle(action: () => Promise<unknown>, done: string) {
    setError(null);
    setMessage(null);
    try {
      await action();
      setMessage(done);
      onChanged?.();
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "the decision could not be saved");
    }
  }

  async function keepAll() {
    if (rows.total === 0) return;
    if (
      !window.confirm(
        `Keep all ${rows.total.toLocaleString()} conflicted paper${rows.total === 1 ? "" : "s"} the search found ` +
          "separate? Each is marked resolved with its identifiers where they are now, and recorded as your decision. " +
          "Merge any that are the same paper first.",
      )
    ) {
      return;
    }
    let resolved = 0;
    try {
      for (let round = 0; round < 200; round++) {
        setBusy(`Keeping separate: ${resolved.toLocaleString()} of ${rows.total.toLocaleString()}`);
        const step = await keepConflictsSeparate(query || undefined);
        resolved += step.resolved;
        if (step.remaining === 0 || step.resolved === 0) break;
      }
      setMessage(`${resolved.toLocaleString()} paper${resolved === 1 ? "" : "s"} kept separate.`);
      pager.reset();
      onChanged?.();
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "stopped part way; press again to continue");
    } finally {
      setBusy(null);
    }
  }

  async function mergeAll() {
    if (rows.pairs === 0) return;
    if (
      !window.confirm(
        (rows.pairs === 1
          ? "Merge the pair the search found into one paper?"
          : `Merge each of the ${rows.pairs.toLocaleString()} pairs the search found into one paper?`) +
          " Each paper is merged with the other paper shown beside it; papers linked through several pairs " +
          "become one, kept as the one with the most identifiers. Each merge is recorded as your decision and is not " +
          "undone automatically. Keep any that are different papers separate first.",
      )
    ) {
      return;
    }
    let merged = 0;
    try {
      for (let round = 0; round < 200; round++) {
        setBusy(`Merging: ${merged.toLocaleString()} so far`);
        const step = await mergeConflictsSamePaper(query || undefined);
        merged += step.merged;
        if (step.remaining === 0 || step.merged === 0) break;
      }
      setMessage(`${merged.toLocaleString()} paper${merged === 1 ? "" : "s"} merged as the same paper.`);
      pager.reset();
      onChanged?.();
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "stopped part way; press again to continue");
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <PanelSection title="Search" hint="Narrows the conflicts below. The bulk action acts on exactly what it finds.">
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            setQuery(draft.trim());
            pager.reset();
          }}
        >
          <label className="space-y-1 text-xs text-muted">
            <span className="block">Title has all of</span>
            <Input
              aria-label="Conflict title has all of"
              placeholder="e.g., fatty acid"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
            />
          </label>
          <Button type="submit" size="sm">
            Search
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => {
              setDraft("");
              setQuery("");
              pager.reset();
            }}
          >
            Clear
          </Button>
        </form>
      </PanelSection>

      <PanelSection
        title="Bulk action"
        hint="Merge when the pairs are the same paper; keep separate when they are different papers."
      >
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-ink">
            {rows.total.toLocaleString()} conflict{rows.total === 1 ? "" : "s"}
            {query ? " match this search" : ""}.
          </span>
          <Button size="sm" disabled={busy !== null || rows.pairs === 0} onClick={() => void mergeAll()}>
            {rows.pairs === 1
              ? "Merge the pair into one paper"
              : `Merge each of the ${rows.pairs.toLocaleString()} pairs into one paper`}
          </Button>
          <Button size="sm" variant="ghost" disabled={busy !== null || rows.total === 0} onClick={() => void keepAll()}>
            Keep all {rows.total.toLocaleString()} separate
          </Button>
          {busy ? <span className="text-xs text-muted">{busy}…</span> : null}
        </div>
      </PanelSection>

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

      <div className="flex flex-wrap items-center gap-3 border-b border-border px-5 py-2.5 text-sm">
        <h3 className="font-semibold text-heading">Identity conflicts</h3>
        <PageCount pager={pager} shown={rows.items.length} total={rows.total} label="identity conflicts" />
      </div>
      {!loaded && !error ? (
        <p className="px-5 py-3 text-sm text-muted" role="status">
          Loading the conflicts…
        </p>
      ) : null}
      {loaded && rows.items.length === 0 ? (
        <div className="px-5 py-3">
          <Empty>{query ? "No conflict matches this search." : "No paper is in conflict."}</Empty>
        </div>
      ) : null}
      <ul className="divide-y divide-border" aria-label="Identity conflicts">
        {rows.items.map((row) => (
          <li key={row.work_id} className="space-y-2 px-5 py-3 text-sm">
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
              {/* The DOIs are in the paper below; the other identifiers stay here. */}
              {row.identifiers
                .filter((identifier) => !identifier.scheme.endsWith("doi"))
                .map((identifier) => (
                <span key={`${identifier.scheme}:${identifier.value}`} className="font-mono">
                  {identifier.scheme.toUpperCase()} {identifier.value}
                </span>
              ))}
              <span className="ml-auto">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() =>
                    void settle(() => keepConflictSeparate(row.work_id), `“${row.title ?? "Untitled"}” kept separate.`)
                  }
                >
                  Keep separate
                </Button>
              </span>
            </div>
            <Paper paper={row} />
            {row.with.length === 0 ? (
              <p className="text-xs text-muted">
                The other paper is no longer in the collection: keeping this one separate settles it.
              </p>
            ) : null}
            {row.with.map((other) => (
              <div key={other.work_id} className="space-y-1 border-l-2 border-border pl-3">
                <p className="text-xs text-muted">
                  {WHY[other.reason] ?? other.reason}
                  {other.identifier ? ` (${other.identifier})` : ""}:
                </p>
                <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
                  <Paper paper={other} />
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() =>
                      void settle(
                        () => mergeWorks(row.work_id, other.work_id, "steward_same_paper"),
                        "Merged: the two are now one paper.",
                      )
                    }
                  >
                    Same paper: merge
                  </Button>
                </div>
              </div>
            ))}
          </li>
        ))}
      </ul>
      <Pager
        pager={pager}
        shown={rows.items.length}
        total={rows.total}
        nextCursor={rows.next_cursor}
        label="identity conflicts"
        sizes={PAGE_SIZES}
      />
    </>
  );
}
