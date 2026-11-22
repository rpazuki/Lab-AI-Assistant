"use client";

import { useCallback, useEffect, useState } from "react";

import { BulkImport } from "@/components/BulkImport";
import { CollectionSetup } from "@/components/CollectionSetup";
import { LiveStatus } from "@/components/ui/LiveStatus";
import { Pager } from "@/components/ui/Pager";
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
import {
  assistedUpload,
  listCandidates,
  listConflicts,
  listPlans,
  mergeWorks,
  setRelevance,
} from "@/lib/api";
import { statusGroup } from "@/lib/statuses";
import { useLiveRefresh } from "@/lib/useLiveRefresh";
import { usePager } from "@/lib/usePager";
import type { AcquisitionPlan, Candidate, DuplicateProposal, Page } from "@/types";

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
 * Collections: scope and discovery, candidate decisions, identity conflicts, and the
 * acquisition queue, in the order a collection is built.
 *
 * One candidate row shows every source assertion behind its canonical fields, so a
 * steward can see *why* a title says what it says before deciding anything. The
 * acquisition queue closes the assisted loop in place: upload here and the original
 * plan resumes.
 */
export function CollectionsClient() {
  // Each list holds one page: a large run is tens of thousands of candidates, and
  // the page refreshes itself while work runs.
  const [candidates, setCandidates] = useState<Page<Candidate>>(EMPTY_PAGE);
  const [plans, setPlans] = useState<Page<AcquisitionPlan>>(EMPTY_PAGE);
  const [planCounts, setPlanCounts] = useState<Record<string, number>>({});
  const [conflictTotal, setConflictTotal] = useState(0);
  const [proposals, setProposals] = useState<Page<DuplicateProposal>>(EMPTY_PAGE);
  const [relevance, setRelevanceFilter] = useState("");
  const [identity, setIdentityFilter] = useState("");
  const [planStatus, setPlanStatus] = useState("");
  const candidatePager = usePager();
  const planPager = usePager();
  const proposalPager = usePager();
  const [error, setError] = useState<string | null>(null);
  const [uploadFor, setUploadFor] = useState<AcquisitionPlan | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [nextCandidates, nextPlans, conflictBody] = await Promise.all([
        listCandidates({
          after: candidatePager.cursor,
          limit: candidatePager.pageSize,
          relevance,
          identity_status: identity,
        }),
        listPlans({ after: planPager.cursor, limit: planPager.pageSize, status: planStatus }),
        listConflicts({ after: proposalPager.cursor, limit: proposalPager.pageSize }),
      ]);
      setCandidates(nextCandidates);
      setPlans(nextPlans);
      setPlanCounts(nextPlans.status_counts);
      setConflictTotal(conflictBody.conflict_total);
      setProposals({
        items: conflictBody.duplicate_proposals,
        next_cursor: conflictBody.next_cursor,
        total: conflictBody.proposal_total,
      });
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "collections could not be loaded");
    }
  }, [
    candidatePager.cursor,
    candidatePager.pageSize,
    planPager.cursor,
    planPager.pageSize,
    proposalPager.cursor,
    proposalPager.pageSize,
    relevance,
    identity,
    planStatus,
  ]);

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

      <Panel title="Identity conflicts and duplicate proposals">
        {conflictTotal === 0 && proposals.total === 0 ? (
          <Empty>No identity decisions are waiting.</Empty>
        ) : null}
        {conflictTotal > 0 ? (
          <Alert tone="danger" className="mb-3">
            {conflictTotal.toLocaleString()} work{conflictTotal === 1 ? "" : "s"} hold
            contradictory strong identifiers and cannot enter a release until resolved.
          </Alert>
        ) : null}
        <ul className="space-y-2 text-sm">
          {proposals.items.map((proposal) => (
            <li
              key={`${proposal.left_work_id}:${proposal.right_work_id}`}
              className="flex flex-wrap items-center gap-2 rounded-control border border-border bg-surface-subtle px-3 py-2"
            >
              <span className="font-mono text-xs">
                {proposal.left_work_id.slice(0, 8)} ↔ {proposal.right_work_id.slice(0, 8)}
              </span>
              <StateChip state={proposal.reason_code} kind="duplicate_reason" />
              <span className="text-xs text-muted">
                similarity {proposal.similarity.toFixed(2)} — title similarity alone never merges
              </span>
              <Button
                variant="ghost"
                size="sm"
                className="ml-auto"
                onClick={async () => {
                  await mergeWorks(
                    proposal.left_work_id,
                    proposal.right_work_id,
                    "reviewer_confirmed_same_work",
                  );
                  live.kick();
                }}
              >
                Merge
              </Button>
            </li>
          ))}
        </ul>
        <div className="-mx-5 -mb-4 mt-3">
          <Pager
            pager={proposalPager}
            shown={proposals.items.length}
            total={proposals.total}
            nextCursor={proposals.next_cursor}
            label="duplicate proposals"
          />
        </div>
      </Panel>

      <Panel title="Candidates" flush aside={<LiveStatus {...live} />}>
        <div className="flex flex-wrap items-center gap-4 border-b border-border px-5 py-2.5">
          <StatusFilter
            label="Relevance"
            kind="relevance"
            value={relevance}
            onChange={(value) => {
              setRelevanceFilter(value);
              candidatePager.reset();
            }}
          />
          <StatusFilter
            label="Identity"
            kind="identity"
            value={identity}
            onChange={(value) => {
              setIdentityFilter(value);
              candidatePager.reset();
            }}
          />
        </div>
        {candidates.items.length === 0 ? (
          <div className="px-5 py-3">
            <Empty>
              {relevance || identity
                ? "No candidates match these filters."
                : "No candidates yet. Run discovery first."}
            </Empty>
          </div>
        ) : null}
        <ul className={`${rowList} text-sm`}>
          {candidates.items.map((candidate) => (
            <li key={candidate.work_id} className={row}>
              <div className="flex flex-wrap items-center gap-2">
                <strong className="text-heading">{candidate.title ?? "Untitled"}</strong>
                <StateChip state={candidate.relevance} kind="relevance" />
                <StateChip state={candidate.identity_status} kind="identity" />
                {candidate.retraction_status !== "none" ? (
                  <StateChip state={candidate.retraction_status} kind="retraction" />
                ) : null}
              </div>
              <p className="mt-1 text-xs text-muted">
                {[candidate.journal, candidate.year, candidate.work_kind]
                  .filter(Boolean)
                  .join(" · ")}
              </p>
              <p className="mt-1 font-mono text-xs text-neutral-strong">
                {candidate.identifiers.map((id) => `${id.scheme}:${id.value}`).join("  ")}
              </p>
              <details className="mt-2 text-xs">
                <summary className="cursor-pointer text-link hover:underline">
                  which sources asserted these fields
                </summary>
                <ul className="mt-2 space-y-0.5 rounded-control bg-surface-subtle px-3 py-2">
                  {Object.entries(candidate.provenance).map(([field, assertionIds]) => (
                    <li key={field}>
                      <span className="font-mono">{field}</span>: {assertionIds.length} assertion
                      {assertionIds.length === 1 ? "" : "s"}
                    </li>
                  ))}
                </ul>
              </details>
              <div className="mt-3 flex gap-2">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={async () => {
                    await setRelevance(candidate.work_id, "included", "steward_included");
                    live.kick();
                  }}
                >
                  Include
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={async () => {
                    await setRelevance(candidate.work_id, "excluded", "out_of_scope");
                    live.kick();
                  }}
                >
                  Exclude
                </Button>
              </div>
            </li>
          ))}
        </ul>
        <Pager
          pager={candidatePager}
          shown={candidates.items.length}
          total={candidates.total}
          nextCursor={candidates.next_cursor}
          label="candidates"
        />
      </Panel>

      <Panel title="Acquisition queue" flush aside={<LiveStatus {...live} />}>
        <div className="flex flex-wrap items-center gap-4 border-b border-border px-5 py-2.5">
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
                <span className="font-mono text-xs">{plan.work_id.slice(0, 8)}</span>
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
        />
      </Panel>

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
    <Panel title={`Assisted acquisition — plan ${plan.plan_id.slice(0, 8)}`}>
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
