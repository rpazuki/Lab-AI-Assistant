"use client";

import { useCallback, useEffect, useState } from "react";

import { BulkImport } from "@/components/BulkImport";
import { CandidateReview } from "@/components/CandidateReview";
import { CollectionSetup } from "@/components/CollectionSetup";
import { DuplicateReview } from "@/components/DuplicateReview";
import { LiveStatus } from "@/components/ui/LiveStatus";
import { CollapsiblePanel } from "@/components/ui/CollapsiblePanel";
import { PageCount, Pager } from "@/components/ui/Pager";
import {
  Alert,
  Button,
  Empty,
  Field,
  Input,
  Panel,
  StateChip,
  row,
  rowList,
} from "@/components/ui/primitives";
import { assistedUpload, fetchIncluded, listPlans } from "@/lib/api";
import { statusGroup } from "@/lib/statuses";
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
                    `${done.requeued.toLocaleString()} fetches will be tried again. ` +
                    `${done.already_tried.toLocaleString()} were already tried by the current fetcher ` +
                    `and stay with a person; ` +
                    `${done.kept_for_a_person.toLocaleString()} can only come from an upload.`,
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
            Plans included papers that have no plan, and tries again the fetches that failed on a
            fault or that the current fetcher has not tried. Pressing it again does nothing twice: a
            planned paper is not planned again, and a paper the current fetcher already tried stays
            with a person.
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
                <span className="text-xs text-muted">
                  routes: {plan.routes.join(" → ")}
                </span>
                {plan.status === "assisted" || plan.status === "failed" ? (
                  <Button variant="ghost" size="sm" className="ml-auto" onClick={() => setUploadFor(plan)}>
                    Upload an authorised copy
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

      <BulkImport
        // The plans on the page above: filter the queue by status to choose from more.
        waiting={plans.items.filter((plan) => plan.status === "assisted" || plan.status === "failed")}
        onImported={() => live.kick()}
      />

      {uploadFor ? (
        <AssistedUploadForm
          plan={uploadFor}
          onDone={() => {
            setUploadFor(null);
            live.kick();
          }}
        />
      ) : null}
    </div>
  );
}

function AssistedUploadForm({ plan, onDone }: { plan: AcquisitionPlan; onDone: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [accessBasis, setAccessBasis] = useState("institutional_subscription_copy");
  const [attestation, setAttestation] = useState("");
  const [result, setResult] = useState<string | null>(null);

  return (
    <Panel title={`Assisted acquisition — ${plan.paper?.title ?? `paper ${plan.work_id.slice(0, 8)}`}`}>
      <Alert tone="info" className="mb-4">
        Automation stopped because no permitted route returned this content. Uploading an
        authorised copy runs the same integrity, malware, identity, and rights checks as an
        automatic fetch, and the queued work resumes.
      </Alert>
      <form
        className="max-w-xl space-y-4"
        onSubmit={async (event) => {
          event.preventDefault();
          if (!file || !attestation) return;
          try {
            const uploaded = await assistedUpload(plan.plan_id, {
              manifestationId: plan.manifestation_ids[0] ?? "",
              accessBasis,
              attestation,
              file,
            });
            setResult(
              `accepted: identity ${uploaded.identity_verified ? "verified" : "unverified"}, ` +
                `malware scan ${uploaded.malware_scan_status}`,
            );
            onDone();
          } catch (caught) {
            setResult(caught instanceof Error ? caught.message : "the upload was rejected");
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
        <Button type="submit" disabled={!file || !attestation}>
          Upload
        </Button>
        {result ? <p className="text-sm text-ink">{result}</p> : null}
      </form>
    </Panel>
  );
}
