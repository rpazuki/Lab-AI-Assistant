"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { AbstractFallback } from "@/components/AbstractFallback";
import { BulkImport } from "@/components/BulkImport";
import { CandidateReview } from "@/components/CandidateReview";
import { CollectionSetup } from "@/components/CollectionSetup";
import { filterSelectClass, PaperFilterFields } from "@/components/PaperFilterFields";
import { DuplicateReview } from "@/components/DuplicateReview";
import { ParseQuality } from "@/components/ParseQuality";
import { LiveStatus } from "@/components/ui/LiveStatus";
import { CollapsiblePanel } from "@/components/ui/CollapsiblePanel";
import { PageCount, Pager } from "@/components/ui/Pager";
import { PanelSection } from "@/components/ui/PanelSection";
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
import {
  QUEUE_ROWS_PER_FILE,
  acquisitionQueueDownloadUrl,
  assistedUpload,
  candidateFacets,
  fetchIncluded,
  fetchSweep,
  listPlans,
} from "@/lib/api";
import { describeStatus, statusGroup } from "@/lib/statuses";
import { useLiveRefresh } from "@/lib/useLiveRefresh";
import { watchTask } from "@/lib/watchTask";
import { PAGE_SIZES, usePager } from "@/lib/usePager";
import type { AcquisitionPlan, CandidateFacets, Page, PlanFilters } from "@/types";

/** Whether a search narrows anything: any field set to something. */
function searchSet(filters: PlanFilters): boolean {
  return Object.values(filters).some(
    (value) => value !== undefined && value !== null && value !== "" && value !== false,
  );
}

/** A link for an identifier a person can follow. */
function identifierLink(scheme: string, value: string): string | null {
  if (scheme === "doi") return `https://doi.org/${value}`;
  if (scheme === "pmid") return `https://pubmed.ncbi.nlm.nih.gov/${value}/`;
  if (scheme === "pmcid") return `https://www.ncbi.nlm.nih.gov/pmc/articles/${value}/`;
  return null;
}

/** Plans the worker is still fetching: nothing has answered yet. */
const FETCHING = new Set(["unplanned", "planned"]);

/**
 * Watch a queued sweep to the end and say what it did.
 *
 * The polling, and what to say when it does not finish, are `watchTask`'s: this
 * is only the sentence the counts make.
 */
export const watchSweep = (taskId: string) =>
  watchTask({
    poll: () => fetchSweep(taskId),
    stillRunning: "Collection progress shows how far it has got.",
    said: (counts) =>
      `Planning queued for any included paper without a plan. ` +
      `${counts.requeued.toLocaleString()} fetches will be tried again` +
      (counts.replanned
        ? `, ${counts.replanned.toLocaleString()} of them papers that now have an open licence. `
        : ". ") +
      `${counts.already_tried.toLocaleString()} were already tried by the current fetcher ` +
      `and stay with a person; ` +
      `${counts.kept_for_a_person.toLocaleString()} can only come from an upload. ` +
      `${counts.degraded_promoted.toLocaleString()} degraded documents now pass the parse ` +
      `limits and go on to extraction; ${counts.still_degraded.toLocaleString()} stay degraded.`,
  });

const EMPTY_PAGE = { items: [], next_cursor: null, total: 0 };

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
  // The search on screen, and the one the list was asked for: typed text waits
  // for Search, a choice from a list applies at once, as in the candidate list.
  const [planDraft, setPlanDraft] = useState<PlanFilters>({});
  const [planFilters, setPlanFilters] = useState<PlanFilters>({});
  const [facets, setFacets] = useState<CandidateFacets | null>(null);
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
        ...planFilters,
      });
      setPlans(nextPlans);
      setPlanCounts(nextPlans.status_counts);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "collections could not be loaded");
    }
  }, [planPager.cursor, planPager.pageSize, planFilters]);

  const fetching = Object.entries(planCounts).some(
    ([status, count]) => FETCHING.has(status) && count > 0,
  );
  const live = useLiveRefresh(refresh, { active: fetching });
  // What the list was asked for, not the words still being typed.
  const downloadFilters = planFilters;
  const searching = searchSet(planFilters);

  // The values the paper fields offer (kinds, sources, languages, years): the
  // candidate list's, since both boxes search the same papers.
  useEffect(() => {
    candidateFacets()
      .then(setFacets)
      .catch(() => setFacets(null));
  }, []);

  function applyPlanSearch(next: PlanFilters) {
    setPlanDraft(next);
    setPlanFilters(next);
    planPager.reset();
  }
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
        <PanelSection
          title="Search"
          hint="Narrows the plans below, with the candidate list's fields. The downloads take exactly what it finds."
        >
          <form
            className="space-y-3"
            onSubmit={(event) => {
              event.preventDefault();
              applyPlanSearch({ ...planDraft, q: planDraft.q?.trim() || undefined });
            }}
          >
            <PaperFilterFields
              draft={planDraft}
              facets={facets}
              onType={(patch) => setPlanDraft((current) => ({ ...current, ...patch }))}
              onChoose={(patch) => applyPlanSearch({ ...planDraft, ...patch })}
              leading={
                <label className="space-y-1 text-xs text-muted">
                  <span className="block">Identifier has</span>
                  <Input
                    aria-label="Identifier has"
                    placeholder="e.g., 10.1038/…, PMC123"
                    value={planDraft.q ?? ""}
                    onChange={(event) =>
                      setPlanDraft((current) => ({ ...current, q: event.target.value }))
                    }
                  />
                </label>
              }
              actions={
                <>
                  <Button type="submit" size="sm">
                    Search
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    disabled={!searchSet(planDraft) && !searching}
                    onClick={() => applyPlanSearch({})}
                  >
                    Clear
                  </Button>
                </>
              }
            >
              <label className="space-y-1">
                <span className="block">Status</span>
                <select
                  aria-label="Filter by status"
                  className={filterSelectClass}
                  value={planDraft.status ?? ""}
                  onChange={(event) =>
                    applyPlanSearch({ ...planDraft, status: event.target.value || undefined })
                  }
                >
                  <option value="">All</option>
                  {Object.keys(statusGroup("acquisition").values).map((state) => (
                    <option key={state} value={state}>
                      {state}
                    </option>
                  ))}
                </select>
              </label>
              <label className="space-y-1">
                <span className="block">Last answer</span>
                <select
                  aria-label="Filter plans by the last answer"
                  className={filterSelectClass}
                  value={planDraft.outcome ?? ""}
                  onChange={(event) =>
                    applyPlanSearch({ ...planDraft, outcome: event.target.value || undefined })
                  }
                >
                  <option value="">Any</option>
                  <option value="none">Never tried (upload only)</option>
                  {Object.keys(statusGroup("acquisition_outcome").values)
                    .filter((outcome) => outcome !== "pending")
                    .map((outcome) => (
                      <option key={outcome} value={outcome}>
                        {outcome.replace(/_/g, " ")}
                      </option>
                    ))}
                </select>
              </label>
            </PaperFilterFields>
          </form>
        </PanelSection>

        <PanelSection
          title="Bulk action"
          hint="Fetch what can be fetched, download what the search finds, and upload many files at once."
        >
          <div className="flex flex-wrap items-center gap-4">
            <Button
              variant="primary"
              size="sm"
              disabled={queuing}
              onClick={async () => {
                setQueuing(true);
                setNotice(null);
                try {
                  const sweep = await fetchIncluded();
                  setError(null);
                  setNotice(
                    "Queued. Every waiting fetch and every degraded document is being read " +
                      "again, which takes a few minutes on a corpus this size. It carries on " +
                      "whether or not you stay on this page.",
                  );
                  setNotice(await watchSweep(sweep.task_id));
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
          </div>
          <div className="space-y-1" aria-label="Download the papers" role="group">
            {plans.total > 0 ? (
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
                <span className="text-ink">
                  {searching
                    ? `Download the ${plans.total.toLocaleString()} ${plans.total === 1 ? "plan" : "plans"} this search finds, on every page:`
                    : `Download all ${plans.total.toLocaleString()} ${plans.total === 1 ? "plan" : "plans"}, on every page:`}
                </span>
                <a
                  className="text-link underline hover:no-underline"
                  href={acquisitionQueueDownloadUrl("csv", downloadFilters)}
                  download
                >
                  One CSV
                </a>
                <a
                  className="text-link underline hover:no-underline"
                  href={acquisitionQueueDownloadUrl("zip", downloadFilters)}
                  download
                >
                  A zip of CSVs, {QUEUE_ROWS_PER_FILE} papers each
                </a>
              </div>
            ) : (
              <p className="text-sm text-muted">Nothing to download: no plan matches this search.</p>
            )}
            <p className="max-w-xl text-xs text-muted">
              For fetching papers by hand. Each row has the paper&apos;s DOI, PubMed and PubMed Central
              links, where the last fetch looked, and its plan id. Search first to narrow it, for
              example to the plans waiting for an upload.
            </p>
          </div>
          <details>
            <summary className="cursor-pointer text-sm text-ink">Import many PDFs at once</summary>
            <div className="mt-2 rounded-control border border-border bg-surface">
              <BulkImport
                embedded
                // The plans shown below: search the queue to choose from others.
                waiting={plans.items.filter(
                  (plan) => plan.status === "assisted" || plan.status === "failed" || plan.status === "partial",
                )}
                onImported={() => live.kick()}
              />
            </div>
          </details>
        </PanelSection>
        {plans.items.length === 0 ? (
          <div className="px-5 py-3">
            <Empty>
              {searching ? "No plan matches this search." : "No acquisition plans yet."}
            </Empty>
          </div>
        ) : null}
        <ul className={`${rowList} text-sm`}>
          {plans.items.map((plan) => (
            <li key={plan.plan_id} className={row}>
              <div className="flex flex-wrap items-center gap-2">
                <strong className="text-heading">{plan.paper?.title ?? "Untitled"}</strong>
                <StateChip state={plan.status} kind="acquisition" />
                {plan.file?.version === "abstract" ? (
                  <span
                    className="rounded-badge border border-border px-1.5 py-0.5 text-xs text-muted"
                    title="No route returned the full text; its abstract stands in until an upload"
                  >
                    abstract only
                  </span>
                ) : null}
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
                {plan.status === "assisted" || plan.status === "failed" || plan.status === "partial" ? (
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

      <CollapsiblePanel title="Collection settings" storageKey="collections.settings" flush>
        <ParseQuality embedded refreshKey={notice} />
        <AbstractFallback embedded onChanged={() => live.kick()} />
      </CollapsiblePanel>

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
