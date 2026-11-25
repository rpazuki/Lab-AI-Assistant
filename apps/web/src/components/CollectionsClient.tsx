"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { BulkImport } from "@/components/BulkImport";
import { CandidateReview } from "@/components/CandidateReview";
import { CollectionSetup } from "@/components/CollectionSetup";
import { DuplicateReview } from "@/components/DuplicateReview";
import { ParseQuality } from "@/components/ParseQuality";
import { LiveStatus } from "@/components/ui/LiveStatus";
import { CollapsiblePanel } from "@/components/ui/CollapsiblePanel";
import { PageCount, Pager } from "@/components/ui/Pager";
import {
  Alert,
  Button,
  Empty,
  Field,
  Input,
  StateChip,
  row,
  rowList,
} from "@/components/ui/primitives";
import { assistedUpload, fetchIncluded, listPlans } from "@/lib/api";
import { describeStatus, statusGroup } from "@/lib/statuses";
import { useLiveRefresh } from "@/lib/useLiveRefresh";
import { PAGE_SIZES, usePager } from "@/lib/usePager";
import type { AcquisitionPlan, Page } from "@/types";

/** A link for an identifier a person can follow. */
function identifierLink(scheme: string, value: string): string | null {
  if (scheme === "doi") return `https://doi.org/${value}`;
  if (scheme === "pmid") return `https://pubmed.ncbi.nlm.nih.gov/${value}/`;
  if (scheme === "pmcid") return `https://www.ncbi.nlm.nih.gov/pmc/articles/${value}/`;
  return null;
}

/** Plans the worker is still fetching: nothing has answered yet. */
const FETCHING = new Set(["unplanned", "planned"]);

const EMPTY_PAGE = { items: [], next_cursor: null, total: 0 };

const selectClass =
  "rounded-control border border-border-strong bg-surface px-2 py-1 text-xs text-ink";

/** A filter over one status group: "All" plus every value the glossary knows. */
function StatusFilter({
  label,
  kind,
  value,
  onChange,
}: {
  label: string;
  kind: "relevance" | "identity" | "acquisition";
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="flex items-center gap-1.5 text-xs text-muted">
      {label}
      <select
        aria-label={`Filter by ${label.toLowerCase()}`}
        className={selectClass}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">All</option>
        {Object.keys(statusGroup(kind).values).map((state) => (
          <option key={state} value={state}>
            {state}
          </option>
        ))}
      </select>
    </label>
  );
}

/**
 * Collections: scope and discovery, duplicates, candidate decisions, and the
 * acquisition queue, in the order a collection is built.
 *
 * Duplicates and candidates are reviewed by rule first (DuplicateReview,
 * CandidateReview) and one by one for what the rules leave. The acquisition queue
 * closes the assisted loop in place: upload here and the original plan resumes.
 */
export function CollectionsClient() {
  // Each list holds one page: a large run is tens of thousands of candidates, and
  // the page refreshes itself while work runs.
  const [plans, setPlans] = useState<Page<AcquisitionPlan>>(EMPTY_PAGE);
  const [planCounts, setPlanCounts] = useState<Record<string, number>>({});
  const [planStatus, setPlanStatus] = useState("");
  const planPager = usePager();
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [queuing, setQueuing] = useState(false);
  const [uploadFor, setUploadFor] = useState<AcquisitionPlan | null>(null);

  const refresh = useCallback(async () => {
    try {
      const nextPlans = await listPlans({
        after: planPager.cursor,
        limit: planPager.pageSize,
        status: planStatus,
      });
      setPlans(nextPlans);
      setPlanCounts(nextPlans.status_counts);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "collections could not be loaded");
    }
  }, [planPager.cursor, planPager.pageSize, planStatus]);

  const fetching = Object.entries(planCounts).some(
    ([status, count]) => FETCHING.has(status) && count > 0,
  );
  const live = useLiveRefresh(refresh, { active: fetching });
  const reload = live.refresh;
  // A new page or filter loads at once rather than at the next scheduled refresh.
  useEffect(() => {
    void reload();
  }, [refresh, reload]);

  return (
    <div className="space-y-4">
      {error ? (
        <Alert tone="danger" role="alert">
          {error}
        </Alert>
      ) : null}

      <CollectionSetup onChanged={() => live.kick()} />

      <DuplicateReview live={live.live} onChanged={() => live.kick()} />

      <CandidateReview live={live.live} />

      <CollapsiblePanel
        title="Acquisition queue"
        storageKey="collections.acquisition"
        flush
        count={
          <PageCount
            pager={planPager}
            shown={plans.items.length}
            total={plans.total}
            label="acquisition plans"
          />
        }
        aside={<LiveStatus {...live} />}
      >
        {notice ? (
          <div className="px-5 pt-3">
            <Alert tone="success" role="status">
              {notice}
            </Alert>
          </div>
        ) : null}
        <div className="flex flex-wrap items-center gap-4 border-b border-border px-5 py-2.5">
          <Button
            variant="primary"
            size="sm"
            disabled={queuing}
            onClick={async () => {
              setQueuing(true);
              setNotice(null);
              try {
                const done = await fetchIncluded();
                setNotice(
                  `Planning queued for any included paper without a plan. ` +
                    `${done.requeued.toLocaleString()} fetches will be tried again` +
                    (done.replanned
                      ? `, ${done.replanned.toLocaleString()} of them papers that now have an open licence. `
                      : ". ") +
                    `${done.already_tried.toLocaleString()} were already tried by the current fetcher ` +
                    `and stay with a person; ` +
                    `${done.kept_for_a_person.toLocaleString()} can only come from an upload. ` +
                    `${done.degraded_promoted.toLocaleString()} degraded documents now pass the parse ` +
                    `limits and go on to extraction; ${done.still_degraded.toLocaleString()} stay degraded.`,
                );
                setError(null);
              } catch (caught) {
                setError(caught instanceof Error ? caught.message : "fetching was not queued");
              } finally {
                setQueuing(false);
              }
              live.kick();
            }}
          >
            Fetch included papers
          </Button>
          <span className="max-w-xl text-xs text-muted">
            Plans included papers that have no plan, tries again the fetches that failed on a fault
            or that no source answered, checks files again that an older identity rule could not tie
            to their paper, gives the open routes to upload-only papers that discovery now records
            an open licence for, and checks degraded documents against the parse limits below. Pressing it
            again does nothing twice: a planned paper is not planned again, and a paper every source
            already answered stays with a person.
          </span>
          <StatusFilter
            label="Status"
            kind="acquisition"
            value={planStatus}
            onChange={(value) => {
              setPlanStatus(value);
              planPager.reset();
            }}
          />
        </div>
        {plans.items.length === 0 ? (
          <div className="px-5 py-3">
            <Empty>{planStatus ? "No plans have this status." : "No acquisition plans yet."}</Empty>
          </div>
        ) : null}
        <ul className={`${rowList} text-sm`}>
          {plans.items.map((plan) => (
            <li key={plan.plan_id} className={row}>
              <div className="flex flex-wrap items-center gap-2">
                <strong className="text-heading">{plan.paper?.title ?? "Untitled"}</strong>
                <StateChip state={plan.status} kind="acquisition" />
                {plan.file?.version === "preprint" ? (
                  <span className="rounded-badge border border-border px-1.5 py-0.5 text-xs text-muted">
                    preprint version
                  </span>
                ) : null}
                {plan.file?.identity_basis === "title" ? (
                  <span
                    className="text-xs text-muted"
                    title="The file names the paper's title, not its identifier"
                  >
                    matched by title
                  </span>
                ) : null}
                <span className="text-xs text-muted">
                  routes: {plan.routes.join(" → ")}
                </span>
                {plan.status === "assisted" || plan.status === "failed" ? (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="ml-auto"
                    aria-expanded={uploadFor?.plan_id === plan.plan_id}
                    onClick={() =>
                      setUploadFor(uploadFor?.plan_id === plan.plan_id ? null : plan)
                    }
                  >
                    {uploadFor?.plan_id === plan.plan_id ? "Close the upload" : "Upload an authorised copy"}
                  </Button>
                ) : null}
              </div>
              <p className="mt-1 flex flex-wrap items-center gap-x-2 text-xs text-muted">
                {[plan.paper?.journal, plan.paper?.year, plan.paper?.first_author]
                  .filter(Boolean)
                  .join(" · ")}
                {(plan.identifiers ?? []).map((identifier) => {
                  const href = identifierLink(identifier.scheme, identifier.value);
                  const text = `${identifier.scheme.toUpperCase()} ${identifier.value}`;
                  return href ? (
                    <a
                      key={text}
                      className="font-mono text-link underline hover:no-underline"
                      href={href}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {text}
                    </a>
                  ) : (
                    <span key={text} className="font-mono">
                      {text}
                    </span>
                  );
                })}
                <span className="font-mono">paper {plan.work_id.slice(0, 8)}</span>
              </p>
              {plan.attempts.length > 0 ? (
                <ul className="mt-2 space-y-0.5 text-xs text-muted">
                  {plan.attempts.map((attempt, index) => (
                    <li key={index} className="flex flex-wrap items-center gap-1">
                      <span className="font-mono">{attempt.route}</span> via {attempt.connector_id} →
                      <StateChip state={attempt.outcome ?? "pending"} kind="acquisition_outcome" />
                      {attempt.http_status ? ` (${attempt.http_status})` : ""}
                      {attempt.note ? ` — ${attempt.note}` : ""}
                    </li>
                  ))}
                </ul>
              ) : null}
              {uploadFor?.plan_id === plan.plan_id ? (
                // In the paper's own row: opened at the foot of the page, it
                // was out of sight and the button seemed to do nothing.
                <AssistedUploadForm
                  plan={plan}
                  onCancel={() => setUploadFor(null)}
                  onDone={(message) => {
                    setUploadFor(null);
                    setNotice(message);
                    live.kick();
                  }}
                />
              ) : null}
            </li>
          ))}
        </ul>
        <Pager
          pager={planPager}
          shown={plans.items.length}
          total={plans.total}
          nextCursor={plans.next_cursor}
          label="acquisition plans"
          sizes={PAGE_SIZES}
        />
      </CollapsiblePanel>

      <ParseQuality refreshKey={notice} />

      <BulkImport
        // The plans on the page above: filter the queue by status to choose from more.
        waiting={plans.items.filter((plan) => plan.status === "assisted" || plan.status === "failed")}
        onImported={() => live.kick()}
      />

    </div>
  );
}

/** Why no route fetched this paper, from its latest attempt. */
function whyUpload(plan: AcquisitionPlan): string {
  const latest = plan.attempts[plan.attempts.length - 1];
  if (!latest?.outcome) {
    return (
      "No route may fetch this paper automatically: its licence is unknown or does not allow " +
      "keeping a copy, so only an upload can bring it in."
    );
  }
  return describeStatus("acquisition_outcome", latest.outcome) ?? `The last route answered ${latest.outcome}.`;
}

function AssistedUploadForm({
  plan,
  onDone,
  onCancel,
}: {
  plan: AcquisitionPlan;
  onDone: (message: string) => void;
  onCancel: () => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [accessBasis, setAccessBasis] = useState("institutional_subscription_copy");
  const [attestation, setAttestation] = useState("");
  const [result, setResult] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    box.current?.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
    box.current?.querySelector<HTMLInputElement>('input[type="file"]')?.focus();
  }, []);

  return (
    <div
      ref={box}
      role="region"
      aria-label="Upload an authorised copy"
      className="mt-3 rounded-control border border-border bg-surface-subtle p-4"
    >
      <Alert tone="info" className="mb-4">
        {whyUpload(plan)} An uploaded copy goes through the same integrity, malware, identity and
        rights checks as a fetched one, and then on to parsing.
      </Alert>
      <form
        className="max-w-xl space-y-4"
        onSubmit={async (event) => {
          event.preventDefault();
          if (!file || !attestation) return;
          setSending(true);
          setResult(null);
          try {
            const uploaded = await assistedUpload(plan.plan_id, {
              manifestationId: plan.manifestation_ids[0] ?? "",
              accessBasis,
              attestation,
              file,
            });
            onDone(
              `Uploaded for “${plan.paper?.title ?? `paper ${plan.work_id.slice(0, 8)}`}”: ` +
                `identity ${uploaded.identity_verified ? "confirmed" : "not confirmed"}, ` +
                `malware scan ${uploaded.malware_scan_status}.`,
            );
          } catch (caught) {
            setResult(caught instanceof Error ? caught.message : "the upload was rejected");
          } finally {
            setSending(false);
          }
        }}
      >
        <Field label="Access basis">
          <Input value={accessBasis} onChange={(event) => setAccessBasis(event.target.value)} />
        </Field>
        <Field label="Attestation">
          <Input
            placeholder="How was this copy obtained?"
            value={attestation}
            onChange={(event) => setAttestation(event.target.value)}
            required
          />
        </Field>
        <input
          aria-label="File"
          type="file"
          onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          className="block text-sm text-muted file:mr-3 file:rounded-control file:border file:border-border-strong file:bg-surface file:px-3 file:py-1.5 file:text-sm file:font-medium file:text-ink hover:file:bg-surface-subtle"
        />
        <div className="flex gap-2">
          <Button type="submit" disabled={!file || !attestation || sending}>
            {sending ? "Uploading…" : "Upload"}
          </Button>
          <Button type="button" variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
        </div>
        {result ? (
          <Alert tone="danger" role="alert">
            {result}
          </Alert>
        ) : null}
      </form>
    </div>
  );
}
