"use client";

import { useCallback, useEffect, useState } from "react";

import { Pager } from "@/components/ui/Pager";
import { Alert, Button, Empty, Field, Panel, StateChip } from "@/components/ui/primitives";
import { listMyAnswerReviews, submitAnswerReview } from "@/lib/api";
import { usePager } from "@/lib/usePager";
import type { AnswerReview, Page } from "@/types";

const label = (name: string) => name.replace(/_/g, " ");

const textareaClass =
  "w-full rounded-control border border-border-strong bg-surface px-3 py-2 text-sm text-ink " +
  "shadow-control focus:border-primary";

/**
 * Answers assigned to me for grading.
 *
 * Each shows the benchmark question, the answer the release gave, and what was
 * measured automatically, then the predecessor's review form: five 1–5 scores
 * (correctness is required), issue flags, confidence, a comment and a suggested
 * answer. A submitted review can be revised; the latest submission counts.
 */
export function AnswerReviewsClient() {
  const [page, setPage] = useState<(Page<AnswerReview> & { to_do: number }) | null>(null);
  const items = page?.items ?? null;
  const pager = usePager(10);
  const [scores, setScores] = useState<string[]>([]);
  const [flags, setFlags] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const body = await listMyAnswerReviews({ after: pager.cursor, limit: pager.pageSize });
      setPage(body);
      setScores(body.scores);
      setFlags(body.flags);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "reviews could not be loaded");
    }
  }, [pager.cursor, pager.pageSize]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Over every review, not only this page: unfinished ones come first.
  const waiting = page?.to_do ?? 0;

  return (
    <div className="space-y-4">
      {error ? (
        <Alert tone="danger" role="alert">
          {error}
        </Alert>
      ) : null}
      {items === null ? (
        <Empty>Loading…</Empty>
      ) : items.length === 0 ? (
        <Panel title="Nothing assigned">
          <Empty>No answer is waiting for your review.</Empty>
        </Panel>
      ) : (
        <>
          <p className="text-sm text-muted">
            {waiting} waiting, {(page?.total ?? 0) - waiting} graded.
          </p>
          {items.map((item) => (
            <ReviewCard key={item.id} item={item} scores={scores} flags={flags} onSaved={refresh} />
          ))}
          {page ? (
            <div className="overflow-hidden rounded-card border border-border bg-surface">
              <Pager
                pager={pager}
                shown={page.items.length}
                total={page.total}
                nextCursor={page.next_cursor}
                label="answer reviews"
              />
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}

function ReviewCard({
  item,
  scores,
  flags,
  onSaved,
}: {
  item: AnswerReview;
  scores: string[];
  flags: string[];
  onSaved: () => Promise<void>;
}) {
  const [given, setGiven] = useState<Record<string, number>>(item.scores);
  const [raised, setRaised] = useState<string[]>(item.flags);
  const [confidence, setConfidence] = useState<number | null>(item.reviewer_confidence);
  const [comment, setComment] = useState(item.comment ?? "");
  const [suggested, setSuggested] = useState(item.suggested_answer ?? "");
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const question = String(item.case?.question ?? "");
  const answer = String(item.case?.answer ?? "");

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      await submitAnswerReview(item.id, {
        scores: given,
        flags: raised,
        reviewer_confidence: confidence,
        comment: comment || null,
        suggested_answer: suggested || null,
      });
      setMessage({ ok: true, text: "Saved." });
      await onSaved();
    } catch (caught) {
      setMessage({ ok: false, text: caught instanceof Error ? caught.message : "not saved" });
    } finally {
      setBusy(false);
    }
  }

  const measured = ["citation_validity", "faithfulness_proxy", "unsupported_numbers", "unsupported"]
    .filter((key) => item.case && key in item.case)
    .map((key) => {
      const value = item.case?.[key];
      return `${label(key)} ${Array.isArray(value) ? value.length : String(value)}`;
    });

  return (
    <Panel
      title={`Case ${item.case_id}`}
      aside={<StateChip state={item.status} kind="answer_review" />}
    >
      <form onSubmit={save} className="space-y-4">
        <div className="space-y-2 text-sm">
          <p className="text-pretitle font-semibold uppercase tracking-wider text-muted">Question</p>
          <p className="text-ink">{question || "—"}</p>
          <p className="text-pretitle font-semibold uppercase tracking-wider text-muted">Answer</p>
          <p className="whitespace-pre-wrap rounded-control border border-border bg-surface-subtle px-3 py-2 text-ink">
            {answer || "—"}
          </p>
          {measured.length ? (
            <p className="font-mono text-xs text-muted">measured: {measured.join(" · ")}</p>
          ) : null}
        </div>

        <fieldset className="grid gap-3 sm:grid-cols-5">
          <legend className="mb-1 text-sm font-medium text-ink">Scores (1 poor – 5 excellent)</legend>
          {scores.map((score) => (
            <label key={score} className="block space-y-1 text-xs">
              <span className="block font-medium capitalize text-ink">
                {label(score)}
                {score === "correctness" ? " *" : ""}
              </span>
              <select
                aria-label={label(score)}
                className="w-full rounded-control border border-border-strong bg-surface px-2 py-1 text-sm"
                value={given[score] ?? ""}
                onChange={(event) =>
                  setGiven((current) => {
                    const next = { ...current };
                    if (event.target.value) next[score] = Number(event.target.value);
                    else delete next[score];
                    return next;
                  })
                }
              >
                <option value="">—</option>
                {[1, 2, 3, 4, 5].map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
          ))}
        </fieldset>

        <fieldset className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
          <legend className="mb-1 text-sm font-medium text-ink">Issues</legend>
          {flags.map((flag) => (
            <label key={flag} className="flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={raised.includes(flag)}
                onChange={() =>
                  setRaised((current) =>
                    current.includes(flag) ? current.filter((f) => f !== flag) : [...current, flag],
                  )
                }
              />
              {label(flag)}
            </label>
          ))}
        </fieldset>

        <div className="grid gap-3 sm:grid-cols-[10rem_1fr]">
          <Field label="Your confidence">
            <select
              className="w-full rounded-control border border-border-strong bg-surface px-2 py-2 text-sm"
              value={confidence ?? ""}
              onChange={(event) => setConfidence(event.target.value ? Number(event.target.value) : null)}
            >
              <option value="">—</option>
              {[1, 2, 3, 4, 5].map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Comment">
            <textarea rows={2} className={textareaClass} value={comment} onChange={(event) => setComment(event.target.value)} />
          </Field>
        </div>
        <Field label="Suggested answer (optional)">
          <textarea rows={3} className={textareaClass} value={suggested} onChange={(event) => setSuggested(event.target.value)} />
        </Field>
        <div className="flex items-center gap-3">
          <Button type="submit" disabled={busy || !given.correctness}>
            {item.status === "submitted" ? "Update review" : "Submit review"}
          </Button>
          {message ? (
            <span role={message.ok ? "status" : "alert"} className={`text-sm ${message.ok ? "text-success-strong" : "text-danger-strong"}`}>
              {message.text}
            </span>
          ) : null}
        </div>
      </form>
    </Panel>
  );
}
