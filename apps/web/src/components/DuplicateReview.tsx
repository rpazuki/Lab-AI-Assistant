"use client";

import { useCallback, useEffect, useState } from "react";

import { LiveStatus } from "@/components/ui/LiveStatus";
import { Pager } from "@/components/ui/Pager";
import { Alert, Button, Empty, Input, Panel } from "@/components/ui/primitives";
import {
  applyMergeRule,
  dismissDuplicate,
  listConflicts,
  mergeDuplicate,
  previewMergeRule,
} from "@/lib/api";
import { useLiveRefresh } from "@/lib/useLiveRefresh";
import { usePager } from "@/lib/usePager";
import type { DuplicateProposal, MergePlan, MergeRule, Page, PaperSummary } from "@/types";

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
  const [pairs, setPairs] = useState<Page<DuplicateProposal>>(EMPTY);
  const [conflicts, setConflicts] = useState(0);
  const [minSimilarity, setMinSimilarity] = useState<number | null>(null);
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
        min_similarity: minSimilarity,
      });
      setPairs({ items: body.duplicate_proposals, next_cursor: body.next_cursor, total: body.proposal_total });
      setConflicts(body.conflict_total);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "the duplicate queue could not be loaded");
    }
  }, [pager.cursor, pager.pageSize, minSimilarity]);

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
      setPlan(await previewMergeRule(rule));
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
      for (let round = 0; remaining > 0 && round < 500; round++) {
        setBusy(`Merging: ${merged.toLocaleString()} of ${plan.works_merged.toLocaleString()} papers`);
        const step = await applyMergeRule(rule);
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
    <Panel title="Duplicates and identity conflicts" flush aside={<LiveStatus {...live} />}>
      <div className="space-y-3 border-b border-border px-5 py-4">
        {conflicts > 0 ? (
          <Alert tone="danger">
            {conflicts.toLocaleString()} work{conflicts === 1 ? "" : "s"} hold contradictory strong
            identifiers and cannot enter a release until resolved.
          </Alert>
        ) : null}
        <h3 className="text-sm font-semibold text-heading">Merge by rule</h3>
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
      </div>

      <div className="flex flex-wrap items-center gap-3 border-b border-border px-5 py-2.5 text-sm">
        <h3 className="font-semibold text-heading">Pairs to review</h3>
        <label className="ml-auto flex items-center gap-1.5 text-xs text-muted">
          Similarity at least
          <select
            aria-label="Show pairs with similarity at least"
            className="rounded-control border border-border-strong bg-surface px-2 py-1 text-xs text-ink"
            value={minSimilarity ?? ""}
            onChange={(event) => {
              setMinSimilarity(event.target.value ? Number(event.target.value) : null);
              pager.reset();
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
      />
    </Panel>
  );
}
