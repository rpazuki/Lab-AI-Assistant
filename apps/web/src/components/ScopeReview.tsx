"use client";

import { useCallback, useEffect, useState } from "react";

import { Pager } from "@/components/ui/Pager";
import { SortHeader } from "@/components/ui/table";
import { Alert, Button, Empty, StateChip, row, rowList } from "@/components/ui/primitives";
import { applyScopeRules, listReviewHistory, previewScopeRules } from "@/lib/api";
import { usePager } from "@/lib/usePager";
import { useTable } from "@/lib/useTable";
import type { Page, PaperSummary, ReviewStep, ScopeRulesPlan, ScopeVersion } from "@/types";

const RELEVANCE_WORDS: Record<string, string> = {
  included: "included",
  excluded: "excluded",
  unreviewed: "left for review",
  included_uncertain: "included as uncertain",
};

const n = (value: number | undefined) => (value ?? 0).toLocaleString();
const plural = (count: number, one: string, many = `${one}s`) =>
  `${count.toLocaleString()} ${count === 1 ? one : many}`;

function title(paper: PaperSummary | undefined): string {
  if (!paper) return "a paper";
  return `“${paper.title ?? "Untitled"}”${paper.year ? ` (${paper.year})` : ""}`;
}

/** "120 included, 4,000 excluded" from a counts object keyed by relevance. */
function byRelevance(counts: Record<string, number>): string {
  const parts = Object.entries(counts)
    .filter(([, count]) => count > 0)
    .map(([relevance, count]) => `${n(count)} ${RELEVANCE_WORDS[relevance] ?? relevance}`);
  return parts.length ? parts.join(", ") : "nothing changed";
}

/** A filter as stored with its step, as words: "title has all of ethanol; years 2000–2010". */
function describeFilter(rule: Record<string, unknown>): string {
  const parts: string[] = [];
  const list = (value: unknown) => (Array.isArray(value) ? value.join(", ") : String(value));
  if (rule.title_words) parts.push(`title has all of ${list(rule.title_words)}`);
  if (rule.exclude_words) parts.push(`title has none of ${list(rule.exclude_words)}`);
  if (rule.subject_terms) parts.push(`subject ${list(rule.subject_terms)}`);
  if (rule.journal) parts.push(`journal ${list(rule.journal)}`);
  if (rule.year_from || rule.year_to) parts.push(`years ${rule.year_from ?? "…"}–${rule.year_to ?? "…"}`);
  if (rule.resource_kinds) parts.push(`kind ${list(rule.resource_kinds)}`);
  if (rule.source) parts.push(`found by ${list(rule.source)}`);
  if (rule.has_abstract !== undefined) parts.push(rule.has_abstract ? "with an abstract" : "without an abstract");
  if (rule.relevance) parts.push(`currently ${list(rule.relevance)}`);
  return parts.length ? parts.join("; ") : "every candidate";
}

/** One step as a sentence a reviewer would say. */
export function describeStep(step: ReviewStep): string {
  const c = step.counts;
  const rule = step.rule;
  switch (step.kind) {
    case "scope_approved":
      return `Approved “${String(rule.name ?? "scope")}” v${String(rule.version ?? "")}; it is frozen from here on.`;
    case "discovery_frozen":
      return `Froze a discovery run: ${plural(c.records_seen ?? 0, "record")} from ${plural(c.connectors ?? 0, "source")}.`;
    case "reconciled":
      return (
        `Grouped the run's records into papers: ${n(c.works_created)} new, ` +
        `${n(c.relevance_included)} included, ${n(c.relevance_excluded)} excluded, ` +
        `${n(c.relevance_review)} left for review, ${plural(c.duplicate_proposals ?? 0, "possible duplicate")}.`
      );
    case "scope_rules_applied":
      return `Applied v${String(rule.version ?? "")}'s rules to the candidates: ${byRelevance(c)}.`;
    case "relevance_by_filter":
      return `Set ${byRelevance(c)} by filter (${describeFilter(rule)}).`;
    case "merge_by_rule":
      return (
        `Merged ${plural(c.works_merged ?? 0, "paper")} by rule ` +
        `(title similarity at least ${Number(rule.min_similarity ?? 1).toFixed(2)}).`
      );
    case "relevance_set":
      return `Set ${title(step.papers[0])} to ${RELEVANCE_WORDS[String(rule.relevance)] ?? String(rule.relevance)}.`;
    case "merged":
      return `Merged two records of ${title(step.papers[0])}.`;
    case "dismissed":
      return `Marked ${title(step.papers[0])} and ${title(step.papers[1])} as different papers.`;
    default:
      return step.kind;
  }
}

/**
 * The review history of one scope version: every step taken while it was the
 * question being answered, newest first. Steps are never edited or removed.
 */
export function ReviewHistory({ scope }: { scope: ScopeVersion }) {
  const pager = usePager(25);
  const [page, setPage] = useState<Page<ReviewStep> | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setPage(
        await listReviewHistory({
          scope_version_id: scope.id,
          after: pager.cursor,
          limit: pager.pageSize,
        }),
      );
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "the history could not be loaded");
    }
  }, [scope.id, pager.cursor, pager.pageSize]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section aria-label={`History of ${scope.name} v${scope.version}`} className="mt-2 rounded-control border border-border">
      <h3 className="border-b border-border px-4 py-2 text-sm font-medium text-heading">
        History of v{scope.version}
      </h3>
      {error ? (
        <div className="px-4 py-2">
          <Alert tone="danger" role="alert">
            {error}
          </Alert>
        </div>
      ) : null}
      {page === null ? (
        <p className="px-4 py-2 text-sm text-muted">Loading…</p>
      ) : page.items.length === 0 ? (
        <div className="px-4">
          <Empty>Nothing has been done under this version yet.</Empty>
        </div>
      ) : (
        <ol className={rowList}>
          {page.items.map((step) => (
            <li key={step.id} className={row}>
              <div className="flex flex-wrap items-center gap-2">
                <StateChip state={step.kind} kind="review_step" />
                <time className="text-xs text-muted" dateTime={step.started_at}>
                  {new Date(step.started_at).toLocaleString()}
                </time>
                <span className="text-xs text-muted">{step.actor ?? "automatic"}</span>
              </div>
              <p className="mt-1 text-sm text-ink">{describeStep(step)}</p>
              {step.note ? <p className="text-xs text-muted">{step.note}</p> : null}
            </li>
          ))}
        </ol>
      )}
      {page ? (
        <Pager
          pager={pager}
          shown={page.items.length}
          total={page.total}
          nextCursor={page.next_cursor}
          label="steps"
        />
      ) : null}
    </section>
  );
}

/**
 * Apply an approved version's concepts and exclusion terms to the candidates.
 *
 * The preview loads as soon as this opens and changes nothing. Candidates a person
 * decided are never changed, and are counted apart so the numbers add up.
 */
export function ScopeRules({ scope, onChanged }: { scope: ScopeVersion; onChanged?: () => void }) {
  const [plan, setPlan] = useState<ScopeRulesPlan | null>(null);
  const [busy, setBusy] = useState<string | null>("Scoring every candidate…");
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const preview = useCallback(async () => {
    setBusy("Scoring every candidate…");
    try {
      setPlan(await previewScopeRules(scope.id));
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "the preview could not be made");
    } finally {
      setBusy(null);
    }
  }, [scope.id]);

  useEffect(() => {
    void preview();
  }, [preview]);

  async function apply() {
    if (!plan || plan.changes === 0) return;
    if (
      !window.confirm(
        `Apply v${scope.version}'s rules: change ${plural(plan.changes, "candidate")} ` +
          `(${transitionWords(plan.transitions)}). Candidates a person decided stay as they are. ` +
          "Each change is recorded with this version, and the whole application is one step in its history.",
      )
    ) {
      return;
    }
    setError(null);
    setMessage(null);
    let applied = 0;
    let remaining = plan.changes;
    let stepId: string | null = null;
    try {
      for (let round = 0; remaining > 0 && round < 500; round++) {
        setBusy(`Applying: ${n(applied)} of ${n(plan.changes)}`);
        const step = await applyScopeRules(scope.id, stepId);
        stepId = step.step_id;
        applied += step.applied;
        remaining = step.remaining;
        if (step.applied === 0) break;
      }
      setMessage(`${plural(applied, "candidate")} changed by v${scope.version}'s rules.`);
      onChanged?.();
      await preview();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "applying stopped part way; preview again to continue");
      setBusy(null);
    }
  }

  return (
    <section aria-label={`Apply the rules of ${scope.name} v${scope.version}`} className="mt-2 space-y-2 rounded-control border border-border px-4 py-3">
      <h3 className="text-sm font-medium text-heading">Apply v{scope.version}&apos;s rules to the candidates</h3>
      {error ? (
        <Alert tone="danger" role="alert">
          {error}
        </Alert>
      ) : null}
      {message ? (
        <Alert tone="success" role="status">
          {message}
        </Alert>
      ) : null}
      {busy ? <p className="text-sm text-muted" aria-live="polite">{busy}</p> : null}
      {plan && !busy ? (
        plan.no_concepts ? (
          <Alert tone="warning" role="status">
            v{scope.version} defines no concepts, so its rules decide nothing. Select New version,
            add concepts (and exclusion terms), approve it, then apply it here.
          </Alert>
        ) : (
          <>
            <p className="text-sm text-ink">
              Of {plural(plan.considered, "candidate")}: {plural(plan.changes, "would change", "would change")}
              {plan.changes ? ` (${transitionWords(plan.transitions)})` : ""}, {n(plan.unchanged)} stay as they are,{" "}
              {n(plan.decided_by_person)} were decided by a person and are left alone
              {plan.not_indexed ? `, ${n(plan.not_indexed)} are not indexed yet` : ""}.
            </p>
            <p className="text-xs text-muted">
              Concepts: {plan.scope.concepts.join("; ")}.{" "}
              {plan.scope.exclusion_terms.length
                ? `Exclusion terms: ${plan.scope.exclusion_terms.join("; ")}. `
                : "No exclusion terms. "}
              Included at {Math.round(plan.scope.include_threshold * 100)}% of concepts or more; excluded at{" "}
              {Math.round(plan.scope.exclude_threshold * 100)}% or less when there is an abstract.
            </p>
            {plan.reasons.length ? <ReasonTable reasons={plan.reasons} /> : null}
            {Object.entries(plan.samples).map(([relevance, papers]) => (
              <div key={relevance}>
                <p className="text-xs font-medium text-muted">
                  For example, would become {RELEVANCE_WORDS[relevance] ?? relevance}:
                </p>
                <ul className="ml-4 list-disc text-xs text-ink">
                  {papers.map((paper) => (
                    <li key={paper.work_id}>
                      {title(paper)}: {paper.reason}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
            <div className="flex gap-2">
              <Button size="sm" disabled={plan.changes === 0} onClick={() => void apply()}>
                {plan.changes === 0 ? "Nothing to change" : `Change ${plural(plan.changes, "candidate")}`}
              </Button>
              <Button size="sm" variant="ghost" onClick={() => void preview()}>
                Preview again
              </Button>
            </div>
          </>
        )
      ) : null}
    </section>
  );
}

/** Why the rules decide what they decide, sortable like every table. */
function ReasonTable({ reasons }: { reasons: ScopeRulesPlan["reasons"] }) {
  const table = useTable(reasons, {
    columns: {
      relevance: (reason) => reason.relevance,
      reason: (reason) => reason.description,
      count: (reason) => reason.count,
    },
  });
  return (
    <table className="data-table">
      <thead>
        <tr>
          <SortHeader table={table} column="relevance">
            The rules say
          </SortHeader>
          <SortHeader table={table} column="reason">
            Because
          </SortHeader>
          <SortHeader table={table} column="count" numeric>
            Candidates
          </SortHeader>
        </tr>
      </thead>
      <tbody>
        {table.rows.map((reason) => (
          <tr key={`${reason.relevance}:${reason.reason}`}>
            <td>
              <StateChip state={reason.relevance} kind="relevance" />
            </td>
            <td className="text-sm">{reason.description}</td>
            <td className="numeric">{n(reason.count)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function transitionWords(transitions: Record<string, number>): string {
  return Object.entries(transitions)
    .map(([key, count]) => {
      const [from, to] = key.split(">");
      return `${n(count)} ${RELEVANCE_WORDS[from ?? ""] ?? from} → ${RELEVANCE_WORDS[to ?? ""] ?? to}`;
    })
    .join(", ");
}
