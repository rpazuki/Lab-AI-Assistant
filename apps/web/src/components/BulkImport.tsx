"use client";

import { useState } from "react";

import { CollapsiblePanel } from "@/components/ui/CollapsiblePanel";
import { Alert, Button, Field, Input, StateChip } from "@/components/ui/primitives";
import { SortHeader, TableSearch } from "@/components/ui/table";
import { assistedUpload, bulkImport, type BulkImportItem } from "@/lib/api";
import { useTable } from "@/lib/useTable";
import type { AcquisitionPlan } from "@/types";

/** Files the API accepts in one request; a bigger drop is sent in turns. */
const BATCH = 200;

/**
 * Drop many PDFs; each is matched to the acquisition waiting for it.
 *
 * Matching is by DOI, PMID or PMCID in the file name or on the first page, never
 * by title, and a file that could belong to two acquisitions is left for a
 * person. "Check matches" stores nothing; "Import" sends every clear match
 * through the same checks as a single upload. A file that did not match can be
 * assigned to a waiting acquisition by hand, here.
 */
export function BulkImport({
  waiting,
  onImported,
}: {
  waiting: AcquisitionPlan[];
  onImported: () => void;
}) {
  const [files, setFiles] = useState<File[]>([]);
  const [accessBasis, setAccessBasis] = useState("institutional_subscription_copy");
  const [attestation, setAttestation] = useState("");
  const [results, setResults] = useState<BulkImportItem[] | null>(null);
  const [dryRun, setDryRun] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);

  function choose(list: FileList | null) {
    const pdfs = Array.from(list ?? []).filter(
      (file) => file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf"),
    );
    setFiles(pdfs);
    setResults(null);
    setError(null);
  }

  async function send(dry: boolean) {
    setBusy(true);
    setError(null);
    try {
      const items: BulkImportItem[] = [];
      for (let start = 0; start < files.length; start += BATCH) {
        const answer = await bulkImport(files.slice(start, start + BATCH), {
          accessBasis,
          attestation,
          dryRun: dry,
        });
        items.push(...answer.items);
      }
      setResults(items);
      setDryRun(dry);
      if (!dry) onImported();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "the batch was not accepted");
    } finally {
      setBusy(false);
    }
  }

  async function assign(item: BulkImportItem, plan: AcquisitionPlan) {
    const file = files.find((candidate) => candidate.name === item.filename);
    if (!file) return;
    try {
      await assistedUpload(plan.plan_id, {
        manifestationId: plan.manifestation_ids[0] ?? "",
        accessBasis,
        attestation,
        file,
      });
      setResults((current) =>
        (current ?? []).map((row) =>
          row === item
            ? { ...row, outcome: "imported", plan_id: plan.plan_id, detail: "assigned by hand" }
            : row,
        ),
      );
      onImported();
    } catch (caught) {
      setResults((current) =>
        (current ?? []).map((row) =>
          row === item
            ? { ...row, detail: caught instanceof Error ? caught.message : "the upload was rejected" }
            : row,
        ),
      );
    }
  }

  const fileTable = useTable(results ?? [], {
    columns: {
      file: (item) => item.filename,
      outcome: (item) => item.outcome,
      identifiers: (item) => item.identifiers.join(" "),
      detail: (item) => item.detail,
    },
    searchText: (item) =>
      `${item.filename} ${item.outcome} ${item.identifiers.join(" ")} ${item.detail ?? ""}`,
  });
  const counts = (results ?? []).reduce<Record<string, number>>((tally, item) => {
    tally[item.outcome] = (tally[item.outcome] ?? 0) + 1;
    return tally;
  }, {});

  return (
    <CollapsiblePanel title="Import many PDFs" storageKey="collections.import">
      <div className="space-y-4">
        <label
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault();
            setDragging(false);
            choose(event.dataTransfer.files);
          }}
          className={`flex cursor-pointer flex-col items-center justify-center gap-1 rounded-card border-2 border-dashed px-4 py-6 text-sm transition-colors ${
            dragging ? "border-accent bg-success-lt" : "border-border-strong bg-surface-subtle"
          }`}
        >
          <span className="font-medium text-ink">
            {files.length
              ? `${files.length} PDF${files.length === 1 ? "" : "s"} chosen`
              : "Drop PDFs here, or choose them"}
          </span>
          <span className="text-xs text-muted">
            Each is matched by a DOI, PMID or PMCID in its name or on its first page.
          </span>
          <input
            aria-label="PDF files"
            type="file"
            accept="application/pdf,.pdf"
            multiple
            className="sr-only"
            onChange={(event) => choose(event.target.files)}
          />
        </label>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Access basis">
            <Input value={accessBasis} onChange={(event) => setAccessBasis(event.target.value)} />
          </Field>
          <Field label="Attestation (applies to every file)">
            <Input
              placeholder="How were these copies obtained?"
              value={attestation}
              onChange={(event) => setAttestation(event.target.value)}
            />
          </Field>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="ghost" disabled={busy || files.length === 0} onClick={() => send(true)}>
            Check matches
          </Button>
          <Button disabled={busy || files.length === 0 || !attestation.trim()} onClick={() => send(false)}>
            {busy ? "Working…" : "Import"}
          </Button>
        </div>
        {error ? (
          <Alert tone="danger" role="alert">
            {error}
          </Alert>
        ) : null}
        {results ? (
          <div className="space-y-2">
            <p role="status" className="text-sm text-ink">
              {dryRun ? "Nothing was stored. " : ""}
              {Object.entries(counts)
                .map(([outcome, count]) => `${count} ${outcome}`)
                .join(", ")}
              .
            </p>
            <div className="overflow-x-auto rounded-control border border-border">
              <TableSearch table={fileTable} label="imported files" placeholder="Filter by file, outcome or identifier" />
              <table className="data-table">
                <thead>
                  <tr>
                    <SortHeader table={fileTable} column="file">
                      File
                    </SortHeader>
                    <SortHeader table={fileTable} column="outcome">
                      Outcome
                    </SortHeader>
                    <SortHeader table={fileTable} column="identifiers">
                      Identifiers
                    </SortHeader>
                    <SortHeader table={fileTable} column="detail">
                      Detail
                    </SortHeader>
                  </tr>
                </thead>
                <tbody>
                  {fileTable.rows.map((item) => (
                    <tr key={item.filename}>
                      <td className="max-w-xs break-words text-xs">{item.filename}</td>
                      <td>
                        <StateChip state={item.outcome} kind="bulk_import" />
                      </td>
                      <td className="font-mono text-xs">{item.identifiers.join(" ") || "—"}</td>
                      <td className="text-xs text-muted">
                        {item.detail ?? (item.plan_id ? `plan ${item.plan_id.slice(0, 8)}` : "—")}
                        {!dryRun &&
                        (item.outcome === "unmatched" || item.outcome === "ambiguous") &&
                        waiting.length > 0 ? (
                          <select
                            aria-label={`Assign ${item.filename}`}
                            className="mt-1 block rounded-control border border-border-strong bg-surface px-2 py-1 text-xs text-ink"
                            defaultValue=""
                            onChange={(event) => {
                              const plan = waiting.find((p) => p.plan_id === event.target.value);
                              if (plan) void assign(item, plan);
                            }}
                          >
                            <option value="" disabled>
                              Assign to a waiting acquisition…
                            </option>
                            {waiting.map((plan) => (
                              <option key={plan.plan_id} value={plan.plan_id}>
                                work {plan.work_id.slice(0, 8)} ({plan.status})
                              </option>
                            ))}
                          </select>
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ) : null}
      </div>
    </CollapsiblePanel>
  );
}
