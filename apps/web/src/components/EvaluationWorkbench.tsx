"use client";

import { useCallback, useEffect, useState } from "react";

import { Alert, Button, Empty, Input, Panel, StateChip } from "@/components/ui/primitives";
import { SortHeader } from "@/components/ui/table";
import {
  assignEvaluationReviews,
  compareEvaluationRuns,
  listEvaluationReviewers,
  listRunReviews,
} from "@/lib/api";
import { useTable } from "@/lib/useTable";
import type { Reviewer, RunComparison, RunReviewSummary } from "@/types";

const label = (name: string) => name.replace(/_/g, " ");

function show(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(3);
  if (Array.isArray(value)) return value.length ? value.join(", ") : "none";
  return String(value);
}

/**
 * Two runs of one benchmark side by side: each metric with its change, each gate,
 * and the cases whose measurements moved. The API refuses runs of different
 * benchmarks, because their cases are different questions.
 */
export function RunComparisonPanel({
  baseId,
  candidateId,
  describe,
  onClose,
}: {
  baseId: string;
  candidateId: string;
  describe: (runId: string) => string;
  onClose: () => void;
}) {
  const [report, setReport] = useState<RunComparison | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    setReport(null);
    setError(null);
    compareEvaluationRuns(baseId, candidateId)
      .then((answer) => current && setReport(answer))
      .catch(
        (caught) =>
          current && setError(caught instanceof Error ? caught.message : "the runs could not be compared"),
      );
    return () => {
      current = false;
    };
  }, [baseId, candidateId]);

  const metrics = useTable(report?.metrics ?? [], {
    columns: {
      metric: (metric) => metric.name,
      base: (metric) => metric.base,
      candidate: (metric) => metric.candidate,
      change: (metric) => metric.delta,
    },
  });

  return (
    <Panel
      title="Comparison"
      aside={
        <Button variant="ghost" size="sm" onClick={onClose}>
          Close
        </Button>
      }
    >
      {error ? (
        <Alert tone="danger" role="alert">
          {error}
        </Alert>
      ) : !report ? (
        <Empty>Comparing…</Empty>
      ) : (
        <div className="space-y-4">
          <p className="text-sm text-ink">
            <strong>Base</strong> {describe(report.base.id)} → <strong>candidate</strong>{" "}
            {describe(report.candidate.id)}
            {report.same_version ? "" : " (different benchmark versions)"}
          </p>
          <table className="data-table">
            <thead>
              <tr>
                <SortHeader table={metrics} column="metric">
                  Metric
                </SortHeader>
                <SortHeader table={metrics} column="base" numeric>
                  Base
                </SortHeader>
                <SortHeader table={metrics} column="candidate" numeric>
                  Candidate
                </SortHeader>
                <SortHeader table={metrics} column="change" numeric>
                  Change
                </SortHeader>
              </tr>
            </thead>
            <tbody>
              {metrics.rows.map((metric) => (
                <tr key={metric.name}>
                  <td className="font-mono text-xs">{metric.name}</td>
                  <td className="numeric">{show(metric.base)}</td>
                  <td className="numeric">{show(metric.candidate)}</td>
                  <td
                    className={`numeric ${
                      metric.delta === null || metric.delta === 0
                        ? "text-muted"
                        : metric.delta > 0
                          ? "text-success-strong"
                          : "text-danger-strong"
                    }`}
                  >
                    {metric.delta === null ? "—" : `${metric.delta > 0 ? "+" : ""}${show(metric.delta)}`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {report.gates.length > 0 ? (
            <ul className="flex flex-wrap gap-2 text-xs">
              {report.gates.map((gate) => (
                <li key={gate.name} className="flex items-center gap-1">
                  <span className="font-mono">{gate.name}</span>
                  <StateChip state={gate.base ? "pass" : "fail"} />→
                  <StateChip state={gate.candidate ? "pass" : "fail"} />
                </li>
              ))}
            </ul>
          ) : null}
          <div>
            <p className="mb-1 text-sm font-medium text-ink">
              {report.changed_cases.length} case{report.changed_cases.length === 1 ? "" : "s"}{" "}
              changed, {report.unchanged_cases} unchanged
            </p>
            <ul className="space-y-1 text-xs">
              {report.changed_cases.map((item) => (
                <li key={item.case_id} className="rounded-control border border-border px-3 py-2">
                  <span className="font-mono font-semibold">{item.case_id}</span>
                  {item.only_in ? (
                    <span className="ml-2 text-muted">only in the {item.only_in} run</span>
                  ) : (
                    <span className="ml-2 text-muted">
                      {Object.entries(item.changes)
                        .map(([field, change]) => `${field}: ${show(change.base)} → ${show(change.candidate)}`)
                        .join(" · ")}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </Panel>
  );
}

/**
 * Human review of a generation run: who grades which sampled answers, and what
 * the submitted grades say together. The grades sit beside the measured metrics
 * and never replace them.
 */
export function AnswerReviewSection({ runId }: { runId: string }) {
  const [reviewers, setReviewers] = useState<Reviewer[]>([]);
  const [chosen, setChosen] = useState<string[]>([]);
  const [sample, setSample] = useState("10");
  const [summary, setSummary] = useState<RunReviewSummary | null>(null);
  const [message, setMessage] = useState<{ tone: "success" | "danger"; text: string } | null>(null);

  const load = useCallback(async () => {
    const [people, reviews] = await Promise.all([
      listEvaluationReviewers().catch(() => []),
      listRunReviews(runId).catch(() => null),
    ]);
    setReviewers(people);
    setSummary(reviews);
  }, [runId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function assign() {
    try {
      const created = await assignEvaluationReviews(runId, {
        reviewer_ids: chosen,
        sample_size: Number(sample),
      });
      setMessage({
        tone: "success",
        text: created.items.length
          ? `${created.items.length} answer${created.items.length === 1 ? "" : "s"} assigned.`
          : "Every case of this run is already assigned.",
      });
      await load();
    } catch (caught) {
      setMessage({ tone: "danger", text: caught instanceof Error ? caught.message : "not assigned" });
    }
  }

  return (
    <section aria-label="Human review" className="space-y-3 rounded-control border border-border bg-surface-subtle p-4">
      <h3 className="text-sm font-semibold text-heading">Human review</h3>
      {summary && summary.assigned > 0 ? (
        <div className="space-y-1 text-xs">
          <p className="text-ink">
            {summary.submitted} of {summary.assigned} answers graded.
          </p>
          <dl className="flex flex-wrap gap-x-4 gap-y-1 font-mono">
            {Object.entries(summary.mean_scores).map(([score, mean]) => (
              <div key={score} className="flex gap-1">
                <dt className="text-muted">{label(score)}</dt>
                <dd className="text-ink">{mean === null ? "—" : mean.toFixed(2)}</dd>
              </div>
            ))}
          </dl>
          <p className="text-muted">
            {Object.entries(summary.flag_counts)
              .filter(([, count]) => count > 0)
              .map(([flag, count]) => `${label(flag)} ×${count}`)
              .join(", ") || "No issue flagged."}
          </p>
        </div>
      ) : (
        <p className="text-xs text-muted">No answer of this run has been assigned for review.</p>
      )}
      {reviewers.length === 0 ? (
        <p className="text-xs text-muted">No member holds the reviewer role.</p>
      ) : (
        <div className="space-y-2">
          <fieldset className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
            <legend className="mb-1 text-xs font-medium text-ink">Reviewers</legend>
            {reviewers.map((person) => (
              <label key={person.user_id} className="flex items-center gap-1.5">
                <input
                  type="checkbox"
                  checked={chosen.includes(person.user_id)}
                  onChange={() =>
                    setChosen((current) =>
                      current.includes(person.user_id)
                        ? current.filter((id) => id !== person.user_id)
                        : [...current, person.user_id],
                    )
                  }
                />
                {person.full_name ?? person.email}
              </label>
            ))}
          </fieldset>
          <div className="flex flex-wrap items-center gap-2">
            <Input
              type="number"
              min={1}
              max={200}
              aria-label="Answers to sample"
              className="w-24 py-1"
              value={sample}
              onChange={(event) => setSample(event.target.value)}
            />
            <Button size="sm" disabled={chosen.length === 0 || !Number(sample)} onClick={assign}>
              Assign answers
            </Button>
          </div>
        </div>
      )}
      {message ? (
        <Alert tone={message.tone} role={message.tone === "danger" ? "alert" : "status"}>
          {message.text}
        </Alert>
      ) : null}
    </section>
  );
}
