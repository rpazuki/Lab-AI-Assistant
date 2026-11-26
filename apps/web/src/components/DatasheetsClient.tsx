"use client";

import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";

import { DeleteButton } from "@/components/ui/DeleteButton";
import { LiveStatus } from "@/components/ui/LiveStatus";
import { Pager } from "@/components/ui/Pager";
import { SortHeader } from "@/components/ui/table";
import { Alert, Button, Empty, Field, Input, Panel, StateChip, row, rowList } from "@/components/ui/primitives";
import {
  ApiError,
  approveExtractionTemplate,
  cancelExtractionRun,
  deleteExtractionRun,
  deleteExtractionTemplate,
  estimateExtraction,
  listExtractionRuns,
  listExtractionTemplates,
  registerExtractionTemplate,
  startExtractionRun,
  workingSheet,
  workingSheetCsvUrl,
} from "@/lib/api";
import { useLiveRefresh } from "@/lib/useLiveRefresh";
import { usePager } from "@/lib/usePager";
import { useTable } from "@/lib/useTable";
import type { ExtractionEstimate, ExtractionRun, ExtractionTemplate, Page, WorkingSheet } from "@/types";

/** A starting point: the worked example's shape, cut down to two targets. */
export const EXAMPLE_TEMPLATE = `template_id: yeast-lipid-datasheet
semantic_version: 1.0.0
title: Yeast lipid datasheet
scientific_purpose: >
  Per-experiment lipid yields and the culture conditions they were measured under.
exclusions: >
  Not for values reported only in a figure without a numeric caption.
cardinality: per_experiment
reviewer_instructions: >
  Check the unit and the basis, not just the number.
targets:
  - target_id: strain
    predicate: strain
    object_kind: entity
    description: The strain the measurement belongs to.
    likely_sections: [Results, Methods, Table]
    required: true
  - target_id: lipid_yield
    predicate: lipid_yield
    object_kind: quantity
    description: Lipid yield as reported, with its unit and basis.
    unit_expectation: g/g
    likely_sections: [Results, Table]
    required: true
`;

const textareaClass =
  "w-full rounded-control border border-border-strong bg-surface px-3 py-2 font-mono text-xs text-ink shadow-control focus:border-primary";
const selectClass =
  "w-full rounded-control border border-border-strong bg-surface px-3 py-2 text-sm text-ink shadow-control";

const number = (value: number) => value.toLocaleString();

/**
 * Datasheets: the template that says what to extract, the runs that extract it,
 * and the working sheet of what extraction proposed.
 *
 * Extraction runs by itself after each document is parsed, with the approved
 * template; a run here covers what is already parsed, or extracts it again. The
 * working sheet shows every current proposal, reviewed or not, for the people
 * building it. The citable datasheet is the wide XLSX in a release's export.
 */
export function DatasheetsClient() {
  const [templates, setTemplates] = useState<ExtractionTemplate[]>([]);
  const [runPage, setRunPage] = useState<Page<ExtractionRun>>({ items: [], next_cursor: null, total: 0 });
  const runs = runPage.items;
  const runPager = usePager(10);
  const [chosen, setChosen] = useState("");
  const [estimate, setEstimate] = useState<ExtractionEstimate | null>(null);
  const [error, setError] = useState<{ message: string; problems: string[] } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const fail = useCallback((caught: unknown, fallback: string) => {
    const problems =
      caught instanceof ApiError && Array.isArray(caught.context.problems)
        ? (caught.context.problems as unknown[]).map(String)
        : [];
    setError({ message: caught instanceof Error ? caught.message : fallback, problems });
  }, []);

  const refresh = useCallback(async () => {
    try {
      const [items, batches] = await Promise.all([
        listExtractionTemplates(),
        listExtractionRuns({ after: runPager.cursor, limit: runPager.pageSize }),
      ]);
      setTemplates(items);
      setRunPage(batches);
      const active = items.find((item) => item.active);
      setChosen((current) => current || active?.key || "");
      setError(null);
    } catch (caught) {
      fail(caught, "datasheets could not be loaded");
    }
  }, [fail, runPager.cursor, runPager.pageSize]);

  // A new estimate is asked for each template.
  useEffect(() => {
    setEstimate(null);
  }, [chosen]);

  const live = useLiveRefresh(refresh, { active: runs.some((run) => !run.finished) });
  const reload = live.refresh;
  // Another page of runs loads at once rather than at the next scheduled refresh.
  useEffect(() => {
    void reload();
  }, [refresh, reload]);

  async function act(action: () => Promise<unknown>, done: string, fallback: string) {
    setBusy(true);
    setNotice(null);
    try {
      await action();
      live.kick();
      await live.refresh();
      setError(null);
      setNotice(done);
    } catch (caught) {
      fail(caught, fallback);
    } finally {
      setBusy(false);
    }
  }

  const active = templates.find((item) => item.active);

  return (
    <div className="space-y-4">
      {error ? (
        <div role="alert" className="space-y-1">
          <Alert tone="danger">{error.message}</Alert>
          {error.problems.length > 0 ? (
            <ul className="list-disc pl-6 font-mono text-xs text-danger-strong">
              {error.problems.map((problem) => (
                <li key={problem}>{problem}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
      {notice ? (
        <Alert tone="success" role="status">
          {notice}
        </Alert>
      ) : null}
      {templates.length > 0 && !active ? (
        <Alert tone="warning" role="status">
          No template is approved, so nothing is extracted after parsing. Approve one below.
        </Alert>
      ) : null}

      <TemplatesPanel
        templates={templates}
        busy={busy}
        onApprove={(template) =>
          act(
            () => approveExtractionTemplate(template.template_id, template.version),
            `${template.key} is approved: extraction now uses it.`,
            "the template was not approved",
          )
        }
        onDelete={(template) =>
          act(
            () => deleteExtractionTemplate(template.template_id, template.version),
            `${template.key} is deleted.`,
            "the template was not deleted",
          )
        }
        onRegister={(source) =>
          act(
            async () => {
              const saved = await registerExtractionTemplate(source);
              setChosen(saved.key);
              return saved;
            },
            "Registered as a draft. Approve it to have extraction use it.",
            "the template was not registered",
          )
        }
      />

      <Panel title="Extract">
        {templates.length === 0 ? (
          <Empty>Register a template first.</Empty>
        ) : (
          <div className="space-y-4">
            <div className="grid gap-3 sm:grid-cols-[1fr_auto_auto_auto] sm:items-end">
              <Field label="Template">
                <select
                  className={selectClass}
                  value={chosen}
                  onChange={(event) => setChosen(event.target.value)}
                >
                  <option value="">Choose a template</option>
                  {templates.map((template) => (
                    <option key={template.key} value={template.key}>
                      {template.key} ({template.status}
                      {template.active ? ", active" : ""})
                    </option>
                  ))}
                </select>
              </Field>
              <Button
                variant="ghost"
                disabled={busy || !chosen}
                onClick={async () => {
                  setBusy(true);
                  try {
                    setEstimate(await estimateExtraction(chosen));
                    setError(null);
                  } catch (caught) {
                    fail(caught, "the estimate failed");
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                Estimate
              </Button>
              <Button
                disabled={busy || !chosen}
                onClick={() =>
                  act(
                    () => startExtractionRun({ template: chosen }),
                    "Extraction is queued for every document this template has not covered.",
                    "the run was not started",
                  )
                }
              >
                Run
              </Button>
              <Button
                variant="danger"
                disabled={busy || !chosen}
                onClick={() => {
                  if (
                    window.confirm(
                      "Extract every eligible document again with this template? Unchanged inputs reuse their earlier results at no cost; changed ones call the model.",
                    )
                  ) {
                    void act(
                      () => startExtractionRun({ template: chosen, rerun: true }),
                      "Re-extraction is queued for every eligible document.",
                      "the re-run was not started",
                    );
                  }
                }}
              >
                Re-run all
              </Button>
            </div>
            {estimate ? <EstimateView estimate={estimate} /> : null}
          </div>
        )}
      </Panel>

      <Panel title="Runs" flush aside={<LiveStatus {...live} />}>
        {runs.length === 0 ? (
          <div className="px-5 py-3">
            <Empty>No extraction run has been started here. Extraction after parsing runs by itself.</Empty>
          </div>
        ) : (
          <ul className={`${rowList} text-sm`}>
            {runs.map((run) => (
              <li key={run.run_id} className={row}>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-xs">{run.run_id.slice(0, 8)}</span>
                  <strong className="text-heading">{run.template}</strong>
                  <StateChip state={run.finished ? "finished" : "running"} kind="extraction_run" />
                  <span className="text-xs text-muted">
                    {Object.entries(run.states)
                      .map(([state, count]) => `${count} ${state}`)
                      .join(", ")}
                  </span>
                  <span className="ml-auto text-xs text-muted">
                    {run.created_at ? new Date(run.created_at).toLocaleString() : ""}
                  </span>
                  {run.finished ? null : (
                    <Button
                      variant="danger"
                      size="sm"
                      onClick={() =>
                        act(
                          () => cancelExtractionRun(run.run_id),
                          "The run's remaining tasks are cancelled.",
                          "the run was not cancelled",
                        )
                      }
                    >
                      Cancel
                    </Button>
                  )}
                  {extractionRunDeletable(run) ? (
                    <DeleteButton
                      what={`extraction run ${run.run_id.slice(0, 8)}`}
                      consequence="No batch of it succeeded, so its papers are queued again by the next run."
                      onDelete={() =>
                        act(
                          () => deleteExtractionRun(run.run_id),
                          `Extraction run ${run.run_id.slice(0, 8)} deleted.`,
                          "the run was not deleted",
                        )
                      }
                    />
                  ) : null}
                </div>
                {run.errors.length > 0 ? (
                  <ul className="mt-1 list-disc pl-6 text-xs text-danger-strong">
                    {run.errors.map((message) => (
                      <li key={message}>{message}</li>
                    ))}
                  </ul>
                ) : null}
              </li>
            ))}
          </ul>
        )}
        <Pager
          pager={runPager}
          shown={runs.length}
          total={runPage.total}
          nextCursor={runPage.next_cursor}
          label="extraction runs"
        />
      </Panel>

      {/* Reloads with the runs: a finished batch adds rows. */}
      <SheetPanel template={chosen} reloadKey={runs} />
    </div>
  );
}

function EstimateView({ estimate }: { estimate: ExtractionEstimate }) {
  return (
    <div role="status" className="rounded-control border border-border bg-surface-subtle px-4 py-3 text-sm">
      <p className="text-ink">
        <strong>{number(estimate.to_extract)}</strong> of {number(estimate.renditions)} eligible
        documents to extract ({number(estimate.already_extracted)} already covered).
      </p>
      <p className="mt-1 font-mono text-xs text-muted">
        about {number(estimate.input_tokens)} input + {number(estimate.output_tokens)} output tokens
        {estimate.cost_usd !== null
          ? ` ≈ $${estimate.cost_usd.toFixed(2)} on ${estimate.model}`
          : " · no price: the configured model provider is not a paid one"}
      </p>
      <p className="mt-1 text-xs text-muted">
        Estimated from {estimate.sampled} sampled document{estimate.sampled === 1 ? "" : "s"}; the
        run records what it actually used.
      </p>
    </div>
  );
}

/** States of a batch that produced nothing to keep. */
const NOTHING_KEPT = new Set(["failed", "dead_letter", "cancelled"]);

/** A run can go once every batch failed or was cancelled; a succeeded batch is kept. */
export function extractionRunDeletable(run: ExtractionRun): boolean {
  return run.finished && Object.keys(run.states).every((state) => NOTHING_KEPT.has(state));
}

function TemplatesPanel({
  templates,
  busy,
  onApprove,
  onDelete,
  onRegister,
}: {
  templates: ExtractionTemplate[];
  busy: boolean;
  onApprove: (template: ExtractionTemplate) => void;
  /** Offered for drafts only; the server also refuses a draft a run has used. */
  onDelete: (template: ExtractionTemplate) => void;
  onRegister: (source: { yaml: string }) => Promise<void>;
}) {
  const [adding, setAdding] = useState(false);
  const [text, setText] = useState("");

  return (
    <Panel
      title="Templates"
      flush
      aside={
        <Button variant="ghost" size="sm" onClick={() => setAdding((value) => !value)}>
          {adding ? "Cancel" : "Add template"}
        </Button>
      }
    >
      {adding ? (
        <div className="space-y-3 border-b border-border p-5">
          <Field label="Template file (YAML or JSON)">
            <input
              type="file"
              accept=".yaml,.yml,.json,application/json,text/yaml"
              onChange={async (event) => {
                const file = event.target.files?.[0];
                if (file) setText(await file.text());
              }}
              className="block text-sm text-ink file:mr-3 file:rounded-control file:border file:border-border-strong file:bg-surface file:px-2.5 file:py-1 file:text-xs"
            />
          </Field>
          <Field label="Or paste it">
            <textarea
              rows={12}
              spellCheck={false}
              className={textareaClass}
              value={text}
              onChange={(event) => setText(event.target.value)}
            />
          </Field>
          <div className="flex flex-wrap gap-2">
            <Button
              disabled={busy || !text.trim()}
              onClick={async () => {
                await onRegister({ yaml: text });
                setText("");
                setAdding(false);
              }}
            >
              Register
            </Button>
            <Button variant="ghost" onClick={() => setText(EXAMPLE_TEMPLATE)}>
              Start from an example
            </Button>
          </div>
          <p className="text-xs text-muted">
            A version cannot change once registered: register a correction as a new{" "}
            <code>semantic_version</code>. Each target says what to find (<code>quantity</code>,{" "}
            <code>text</code>, <code>entity</code>, <code>boolean</code> or <code>interval</code>),
            where it is likely, and its unit. Bibliographic fields are{" "}
            <code>deterministic_metadata</code>: they come from the sources, never from a model.
          </p>
        </div>
      ) : null}
      {templates.length === 0 ? (
        <div className="px-5 py-3">
          <Empty>No template is registered, so nothing is extracted.</Empty>
        </div>
      ) : (
        <ul className={`${rowList} text-sm`}>
          {templates.map((template) => (
            <li key={template.key} className={row}>
              <div className="flex flex-wrap items-center gap-2">
                <strong className="text-heading">{template.title}</strong>
                <span className="font-mono text-xs text-muted">{template.key}</span>
                <StateChip state={template.status} kind="approval" />
                {template.active ? <StateChip state="active" kind="flag" /> : null}
                <span className="ml-auto text-xs text-muted">
                  {template.targets.length} target{template.targets.length === 1 ? "" : "s"} ·{" "}
                  {template.cardinality.replace(/_/g, " ")}
                </span>
                {template.active ? null : (
                  <Button size="sm" disabled={busy} onClick={() => onApprove(template)}>
                    Approve
                  </Button>
                )}
                {template.status === "draft" ? (
                  <DeleteButton
                    what={`template ${template.key}`}
                    consequence="It is a draft; it can be deleted only if no run used it and no value was extracted with it."
                    onDelete={() => onDelete(template)}
                  />
                ) : null}
              </div>
              {template.scientific_purpose ? (
                <p className="mt-1 text-muted">{template.scientific_purpose}</p>
              ) : null}
              <p className="mt-1 font-mono text-xs text-ink">
                {template.targets
                  .map(
                    (target) =>
                      `${target.target_id}${target.unit_expectation ? ` [${target.unit_expectation}]` : ""}${
                        target.required ? "*" : ""
                      }`,
                  )
                  .join(" · ")}
              </p>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

/**
 * The working sheet, a page of rows at a time. The search runs on the server over
 * every row, not only the page on screen; the counts are over the whole sheet.
 */
function SheetPanel({ template, reloadKey }: { template: string; reloadKey: unknown }) {
  const pager = usePager();
  const [sheet, setSheet] = useState<WorkingSheet | null>(null);
  const [typed, setTyped] = useState("");
  const [query, setQuery] = useState("");
  const { cursor, pageSize, reset } = pager;

  // A new template starts the sheet again at its first page, unsearched.
  useEffect(() => {
    setTyped("");
    setQuery("");
    reset();
  }, [template, reset]);

  useEffect(() => {
    if (!template) {
      setSheet(null);
      return;
    }
    let current = true;
    workingSheet(template, { after: cursor, limit: pageSize, q: query || null })
      .then((answer) => current && setSheet(answer))
      .catch(() => current && setSheet(null));
    return () => {
      current = false;
    };
  }, [template, cursor, pageSize, query, reloadKey]);

  const columns = useMemo(
    () => (sheet?.wide.columns ?? []).filter((column) => column !== "revision_ids"),
    [sheet],
  );
  const table = useTable(sheet?.wide.rows ?? [], {
    columns: Object.fromEntries(
      columns.map((column) => [column, (row: Record<string, string>) => row[column]]),
    ),
  });

  function search(event: FormEvent) {
    event.preventDefault();
    setQuery(typed.trim());
    reset();
  }

  return (
    <Panel
      title="Working sheet"
      flush
      aside={
        template ? (
          <div className="flex gap-3 text-sm">
            <a className="text-link underline hover:no-underline" href={workingSheetCsvUrl("wide", template)} download>
              Wide CSV
            </a>
            <a className="text-link underline hover:no-underline" href={workingSheetCsvUrl("long", template)} download>
              Long CSV
            </a>
          </div>
        ) : null
      }
    >
      {!sheet || sheet.claims === 0 ? (
        <div className="px-5 py-3">
          <Empty>
            {template ? "Nothing extracted with this template yet." : "Choose a template to see its sheet."}
          </Empty>
        </div>
      ) : (
        <>
          <p className="px-5 pt-3 text-xs text-muted">
            {sheet.claims} proposals ·{" "}
            {Object.entries(sheet.review_status)
              .map(([status, count]) => `${count} ${status}`)
              .join(", ")}
            . Unreviewed values are proposals, not findings; the Review page is where they become
            either.
          </p>
          <form
            role="search"
            aria-label="Search the working sheet"
            onSubmit={search}
            className="mt-2 flex flex-wrap items-center gap-3 border-b border-border px-5 py-2.5"
          >
            <Input
              type="search"
              aria-label="Search working sheet rows"
              placeholder="Words in any column, e.g., strain, value, unit"
              className="w-full max-w-xs"
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
            />
            <Button type="submit" variant="ghost" size="sm">
              Search
            </Button>
            {query ? (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => {
                  setTyped("");
                  setQuery("");
                  reset();
                }}
              >
                Clear
              </Button>
            ) : null}
          </form>
          {sheet.wide.total === 0 ? (
            <div className="px-5 py-3">
              <Empty>No row holds every word.</Empty>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="data-table">
                <thead>
                  <tr>
                    {columns.map((column) => (
                      <SortHeader key={column} table={table} column={column}>
                        {column.replace(/_/g, " ")}
                      </SortHeader>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {table.rows.map((item, index) => (
                    <tr key={`${item.work_id}:${item.experiment_id}:${index}`}>
                      {columns.map((column) => (
                        <td
                          key={column}
                          className={column.endsWith("_id") ? "font-mono text-xs text-muted" : "text-sm"}
                        >
                          {column.endsWith("_id") ? (item[column] ?? "").slice(0, 8) : item[column]}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <Pager
            pager={pager}
            shown={sheet.wide.rows.length}
            total={sheet.wide.total}
            nextCursor={sheet.wide.next_cursor}
            label="working sheet rows"
          />
        </>
      )}
    </Panel>
  );
}
