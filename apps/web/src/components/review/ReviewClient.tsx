"use client";

import { useCallback, useEffect, useState } from "react";

import { Button, Empty, Input, Panel, StateChip } from "@/components/ui/primitives";
import { decideReview, listReviewQueue } from "@/lib/api";
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

  const refresh = useCallback(async () => {
    try {
      setItems(await listReviewQueue(includeInvalid));
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
        aside={
          <label className="flex items-center gap-2 text-xs">
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
          <p role="alert" className="mb-3 text-sm text-red-700">
            {error}
          </p>
        ) : null}
        {items.length === 0 ? <Empty>Nothing is waiting for review.</Empty> : null}
        <ul className="space-y-4">
          {items.map((item) => (
            <li key={item.revision_id} className="rounded border border-neutral-200 p-3">
              <div className="flex flex-wrap items-baseline gap-2">
                <span className="font-mono text-xs text-neutral-500">{item.predicate}</span>
                <strong className="text-sm">{String(item.value.verbatim ?? "—")}</strong>
                <StateChip state={item.validation_status} />
                <StateChip state={item.abstention_reason} />
                <StateChip state={item.proposal_source} />
                <span className="ml-auto font-mono text-xs text-neutral-400">
                  rev {item.revision_number}
                </span>
              </div>

              {item.validation_issues.length > 0 ? (
                <ul className="mt-2 list-disc pl-5 text-xs text-red-700">
                  {item.validation_issues.map((issue) => (
                    <li key={issue}>{issue}</li>
                  ))}
                </ul>
              ) : null}

              {item.evidence.length > 0 ? (
                <ul className="mt-2 space-y-1 text-xs">
                  {item.evidence.map((span) => (
                    <li key={span.span_id} className="rounded bg-neutral-50 p-2">
                      <p className="italic">“{span.quote_preview}”</p>
                      <p className="mt-1 font-mono text-neutral-500">
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
                <p className="mt-2 text-xs text-amber-800">
                  No verified evidence span is attached to this proposal.
                </p>
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
                  className="max-w-[12rem]"
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
    </div>
  );
}
