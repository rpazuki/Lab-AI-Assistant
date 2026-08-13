"use client";

import { useCallback, useEffect, useState } from "react";

import { Button, Empty, Input, Panel, StateChip } from "@/components/ui/primitives";
import {
  assistedUpload,
  listCandidates,
  listConflicts,
  listPlans,
  mergeWorks,
  setRelevance,
} from "@/lib/api";
import type { AcquisitionPlan, Candidate } from "@/types";

/**
 * Collections: candidate decisions, identity conflicts, and the acquisition queue.
 *
 * One candidate row shows every source assertion behind its canonical fields, so a
 * steward can see *why* a title says what it says before deciding anything. The
 * acquisition queue closes the assisted loop in place: upload here and the original
 * plan resumes.
 */
export function CollectionsClient() {
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [plans, setPlans] = useState<AcquisitionPlan[]>([]);
  const [conflicts, setConflicts] = useState<{ work_id: string }[]>([]);
  const [proposals, setProposals] = useState<
    { left_work_id: string; right_work_id: string; similarity: number; reason_code: string }[]
  >([]);
  const [error, setError] = useState<string | null>(null);
  const [uploadFor, setUploadFor] = useState<AcquisitionPlan | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [nextCandidates, nextPlans, conflictBody] = await Promise.all([
        listCandidates(),
        listPlans(),
        listConflicts(),
      ]);
      setCandidates(nextCandidates);
      setPlans(nextPlans);
      setConflicts(conflictBody.conflicts);
      setProposals(conflictBody.duplicate_proposals);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "collections could not be loaded");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div className="space-y-4">
      {error ? (
        <p role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">
          {error}
        </p>
      ) : null}

      <Panel title="Identity conflicts and duplicate proposals">
        {conflicts.length === 0 && proposals.length === 0 ? (
          <Empty>No identity decisions are waiting.</Empty>
        ) : null}
        {conflicts.length > 0 ? (
          <p className="mb-2 text-sm">
            {conflicts.length} work{conflicts.length === 1 ? "" : "s"} hold contradictory strong
            identifiers and cannot enter a release until resolved.
          </p>
        ) : null}
        <ul className="space-y-2 text-sm">
          {proposals.map((proposal) => (
            <li
              key={`${proposal.left_work_id}:${proposal.right_work_id}`}
              className="flex flex-wrap items-center gap-2 rounded border border-neutral-200 p-2"
            >
              <span className="font-mono text-xs">
                {proposal.left_work_id.slice(0, 8)} ↔ {proposal.right_work_id.slice(0, 8)}
              </span>
              <StateChip state={proposal.reason_code} />
              <span className="text-xs text-neutral-500">
                similarity {proposal.similarity.toFixed(2)} — title similarity alone never merges
              </span>
              <Button
                variant="ghost"
                onClick={async () => {
                  await mergeWorks(
                    proposal.left_work_id,
                    proposal.right_work_id,
                    "reviewer_confirmed_same_work",
                  );
                  void refresh();
                }}
              >
                Merge
              </Button>
            </li>
          ))}
        </ul>
      </Panel>

      <Panel title="Candidates">
        {candidates.length === 0 ? <Empty>No candidates yet. Run discovery first.</Empty> : null}
        <ul className="space-y-3 text-sm">
          {candidates.map((candidate) => (
            <li key={candidate.work_id} className="rounded border border-neutral-200 p-3">
              <div className="flex flex-wrap items-baseline gap-2">
                <strong>{candidate.title ?? "Untitled"}</strong>
                <StateChip state={candidate.relevance} />
                <StateChip state={candidate.identity_status} />
                {candidate.retraction_status !== "none" ? (
                  <StateChip state={candidate.retraction_status} />
                ) : null}
              </div>
              <p className="mt-1 text-xs text-neutral-500">
                {[candidate.journal, candidate.year, candidate.work_kind]
                  .filter(Boolean)
                  .join(" · ")}
              </p>
              <p className="mt-1 font-mono text-xs text-neutral-600">
                {candidate.identifiers.map((id) => `${id.scheme}:${id.value}`).join("  ")}
              </p>
              <details className="mt-2 text-xs">
                <summary className="cursor-pointer text-neutral-500">
                  which sources asserted these fields
                </summary>
                <ul className="mt-1 space-y-0.5">
                  {Object.entries(candidate.provenance).map(([field, assertionIds]) => (
                    <li key={field}>
                      <span className="font-mono">{field}</span>: {assertionIds.length} assertion
                      {assertionIds.length === 1 ? "" : "s"}
                    </li>
                  ))}
                </ul>
              </details>
              <div className="mt-2 flex gap-2">
                <Button
                  variant="ghost"
                  onClick={async () => {
                    await setRelevance(candidate.work_id, "included", "steward_included");
                    void refresh();
                  }}
                >
                  Include
                </Button>
                <Button
                  variant="ghost"
                  onClick={async () => {
                    await setRelevance(candidate.work_id, "excluded", "out_of_scope");
                    void refresh();
                  }}
                >
                  Exclude
                </Button>
              </div>
            </li>
          ))}
        </ul>
      </Panel>

      <Panel title="Acquisition queue">
        {plans.length === 0 ? <Empty>No acquisition plans yet.</Empty> : null}
        <ul className="space-y-2 text-sm">
          {plans.map((plan) => (
            <li key={plan.plan_id} className="rounded border border-neutral-200 p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-xs">{plan.work_id.slice(0, 8)}</span>
                <StateChip state={plan.status} />
                <span className="text-xs text-neutral-500">
                  routes: {plan.routes.join(" → ")}
                </span>
                {plan.status === "assisted" || plan.status === "failed" ? (
                  <Button variant="ghost" onClick={() => setUploadFor(plan)}>
                    Upload an authorised copy
                  </Button>
                ) : null}
              </div>
              {plan.attempts.length > 0 ? (
                <ul className="mt-2 space-y-0.5 text-xs text-neutral-600">
                  {plan.attempts.map((attempt, index) => (
                    <li key={index}>
                      <span className="font-mono">{attempt.route}</span> via {attempt.connector_id}{" "}
                      → {attempt.outcome ?? "pending"}
                      {attempt.http_status ? ` (${attempt.http_status})` : ""}
                      {attempt.note ? ` — ${attempt.note}` : ""}
                    </li>
                  ))}
                </ul>
              ) : null}
            </li>
          ))}
        </ul>
      </Panel>

      {uploadFor ? (
        <AssistedUploadForm
          plan={uploadFor}
          onDone={() => {
            setUploadFor(null);
            void refresh();
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
      <p className="mb-3 text-sm text-neutral-600">
        Automation stopped because no permitted route returned this content. Uploading an
        authorised copy runs the same integrity, malware, identity, and rights checks as an
        automatic fetch, and the queued work resumes.
      </p>
      <form
        className="space-y-3"
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
        <label className="block text-sm">
          Access basis
          <Input value={accessBasis} onChange={(event) => setAccessBasis(event.target.value)} />
        </label>
        <label className="block text-sm">
          Attestation
          <Input
            placeholder="How was this copy obtained?"
            value={attestation}
            onChange={(event) => setAttestation(event.target.value)}
            required
          />
        </label>
        <input
          aria-label="File"
          type="file"
          onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          className="block text-sm"
        />
        <Button type="submit" disabled={!file || !attestation}>
          Upload
        </Button>
        {result ? <p className="text-sm">{result}</p> : null}
      </form>
    </Panel>
  );
}
