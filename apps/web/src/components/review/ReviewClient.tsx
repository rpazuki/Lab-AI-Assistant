"use client";

import { useCallback, useEffect, useState } from "react";

import {
  Alert,
  Button,
  Empty,
  Input,
  Panel,
  StateChip,
  row,
  rowList,
} from "@/components/ui/primitives";
import { correctionRate, decideReview, listReviewQueue } from "@/lib/api";
import type { ReviewItem } from "@/types";

/**
 * Claim review.
 *
 * Every row shows the proposed value, the exact evidence with its locator, and the
 * validation issues that a mechanical validator already found. Accepting is only
 * offered for a valid proposal: an invalid one must be corrected or rejected,
 * because accepting unverified evidence is the failure this whole layer exists to
 * prevent.
 */
export function ReviewClient() {
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [includeInvalid, setIncludeInvalid] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [corrections, setCorrections] = useState<Record<string, string>>({});
  const [rates, setRates] = useState<Record<string, number> | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [queue, measured] = await Promise.all([
        listReviewQueue(includeInvalid),
        // The rate is a secondary measure: the queue still loads without it.
        correctionRate().catch(() => null),
      ]);
      setItems(queue);
      setRates(measured);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "the review queue could not be loaded");
    }
  }, [includeInvalid]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function decide(item: ReviewItem, decision: "accept" | "reject" | "correct" | "defer") {
    setBusy(item.revision_id);
    try {
      await decideReview(item.revision_id, {
        decision,
        reason_code:
          decision === "accept"
            ? "evidence_supports_value"
            : decision === "reject"
              ? "evidence_does_not_support_value"
              : decision === "defer"
                ? "needs_a_second_opinion"
                : "reviewer_correction",
        // Optimistic concurrency: a stale read is refused rather than overwriting
        // someone else's correction.
        expected_revision_number: item.revision_number,
        corrected_value:
          decision === "correct"
            ? { ...item.value, verbatim: corrections[item.revision_id] ?? "" }
            : undefined,
      });
      await refresh();
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "the decision was not recorded");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-4">
      <Panel
        title="Claim review"
        flush
        aside={
          <label className="flex items-center gap-2 text-xs text-muted">
            <input
              type="checkbox"
              checked={includeInvalid}
              onChange={(event) => setIncludeInvalid(event.target.checked)}
            />
            show proposals that failed validation
          </label>
        }
      >
        {error ? (
          <div className="px-5 pt-4">
            <Alert tone="danger" role="alert">
              {error}
            </Alert>
          </div>
        ) : null}
        {items.length === 0 ? (
          <div className="px-5 py-3">
            <Empty>Nothing is waiting for review.</Empty>
          </div>
        ) : null}
        <ul className={rowList}>
          {items.map((item) => (
            <li key={item.revision_id} className={row}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-xs text-muted">{item.predicate}</span>
                <strong className="text-sm text-heading">{String(item.value.verbatim ?? "—")}</strong>
                <StateChip state={item.validation_status} kind="claim_validation" />
                <StateChip state={item.abstention_reason} kind="abstention" />
                <StateChip state={item.proposal_source} kind="proposal_source" />
                <span className="ml-auto font-mono text-xs text-muted">
                  rev {item.revision_number}
                </span>
              </div>

              {item.validation_issues.length > 0 ? (
                <ul className="mt-2 list-disc rounded-control bg-danger-lt py-2 pl-8 pr-3 text-xs text-danger-strong">
                  {item.validation_issues.map((issue) => (
                    <li key={issue}>{issue}</li>
                  ))}
                </ul>
              ) : null}

              {item.evidence.length > 0 ? (
                <ul className="mt-2 space-y-1 text-xs">
                  {item.evidence.map((span) => (
                    <li
                      key={span.span_id}
                      className="rounded-control border-l-2 border-accent bg-surface-subtle px-3 py-2"
                    >
                      <p className="italic text-ink">“{span.quote_preview}”</p>
                      <p className="mt-1 font-mono text-muted">
                        {span.node_id} · normalisation v{span.normalisation_version} ·{" "}
                        {Object.entries(span.locator)
                          .filter(([key]) => key !== "node_id")
                          .map(([key, value]) => `${key}=${String(value)}`)
                          .join(" ")}
                      </p>
                    </li>
                  ))}
                </ul>
              ) : (
                <Alert tone="warning" className="mt-2 text-xs">
                  No verified evidence span is attached to this proposal.
                </Alert>
              )}

              <div className="mt-3 flex flex-wrap items-center gap-2">
                <Button
                  disabled={busy === item.revision_id || item.validation_status !== "valid"}
                  onClick={() => decide(item, "accept")}
                >
                  Accept
                </Button>
                <Input
                  aria-label={`Corrected value for ${item.predicate}`}
                  placeholder="corrected value"
                  className="max-w-[14rem]"
                  value={corrections[item.revision_id] ?? ""}
                  onChange={(event) =>
                    setCorrections((current) => ({
                      ...current,
                      [item.revision_id]: event.target.value,
                    }))
                  }
                />
                <Button
                  variant="ghost"
                  disabled={busy === item.revision_id || !(corrections[item.revision_id] ?? "")}
                  onClick={() => decide(item, "correct")}
                >
                  Correct
                </Button>
                <Button
                  variant="ghost"
                  disabled={busy === item.revision_id}
                  onClick={() => decide(item, "defer")}
                >
                  Defer
                </Button>
                <Button
                  variant="danger"
                  disabled={busy === item.revision_id}
                  onClick={() => decide(item, "reject")}
                >
                  Reject
                </Button>
              </div>
            </li>
          ))}
        </ul>
      </Panel>

      <CorrectionRates rates={rates} />
    </div>
  );
}

/**
 * How often reviewers change what the extractor proposed, per predicate.
 *
 * It is the measured input to double review: a predicate whose correction rate
 * is high is the one worth a second reviewer, so the number is shown where the
 * reviewing happens.
 */
export function CorrectionRates({ rates }: { rates: Record<string, number> | null }) {
  const entries = Object.entries(rates ?? {}).sort(([, a], [, b]) => b - a);
  return (
    <Panel title="Correction rate by predicate" flush>
      {entries.length === 0 ? (
        <div className="px-5 py-3">
          <Empty>No reviewed claims yet, so there is nothing to measure.</Empty>
        </div>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>Predicate</th>
              <th className="numeric">Corrected</th>
            </tr>
          </thead>
          <tbody>
            {entries.map(([predicate, rate]) => (
              <tr key={predicate}>
                <td className="font-mono text-xs">{predicate}</td>
                <td className="numeric">{(rate * 100).toFixed(1)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
