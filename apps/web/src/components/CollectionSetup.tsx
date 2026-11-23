"use client";

import { useCallback, useState } from "react";

import { QueryHelper } from "@/components/QueryHelper";
import { ReviewHistory, ScopeRules } from "@/components/ScopeReview";
import { DeleteButton } from "@/components/ui/DeleteButton";
import { LiveStatus } from "@/components/ui/LiveStatus";
import { SortHeader } from "@/components/ui/table";
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
  approveScope,
  checkScopeGate,
  createScope,
  deleteDiscoveryRun,
  deleteScopeDraft,
  freezeDiscovery,
  listDiscoveryRuns,
  listScopes,
  startDiscovery,
  updateScopeDraft,
} from "@/lib/api";
import { useLiveRefresh } from "@/lib/useLiveRefresh";
import { useTable } from "@/lib/useTable";
import type { DiscoveryRun, ScopeGateReport, ScopeVersion } from "@/types";

/** The sources this installation registers. Each takes its own query language. */
export const CONNECTORS = [
  { id: "pubmed", label: "PubMed", example: '"Yarrowia lipolytica"[Title/Abstract] AND lipid' },
  { id: "europepmc", label: "Europe PMC", example: 'TITLE_ABS:"Yarrowia lipolytica" AND lipid' },
  { id: "crossref", label: "Crossref", example: "Yarrowia lipolytica lipid" },
  { id: "openalex", label: "OpenAlex", example: "Yarrowia lipolytica lipid" },
  { id: "datacite", label: "DataCite", example: "Yarrowia lipolytica" },
] as const;

const textareaClass =
  "w-full rounded-control border border-border-strong bg-surface px-3 py-2 text-sm text-ink " +
  "shadow-control placeholder:text-muted focus:border-primary focus-visible:outline-offset-0";

const lines = (text: string) =>
  text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);

/** A run can be frozen until it has been frozen, or has ended without results. */
const FREEZABLE = new Set(["planned", "running", "degraded"]);
/** A run whose sources are still being searched. */
const SEARCHING = new Set(["planned", "running"]);
/** A run that stopped short. A frozen run can go only if it found nothing. */
const STOPPED = new Set(["degraded", "failed", "cancelled"]);

/**
 * Whether the server would delete this run. A frozen run with records has
 * candidates that decisions and releases point at; the server refuses the rest
 * it can see and the page cannot (queued work, a later run of the same search).
 */
function runDeletable(run: DiscoveryRun): boolean {
  if (STOPPED.has(run.status)) return true;
  return run.status === "frozen" && run.connectors.every((state) => state.records_seen === 0);
}

/**
 * Setting a collection up: its scope, and the discovery runs that search it.
 *
 * A scope has to pass its gate (inclusion criteria, intended uses, every source
 * query compiled) and be approved before discovery may run against it. Until it
 * is approved nothing refers to it, so a draft can be corrected in place, saved
 * as a new version, or deleted. An approved version is immutable: discovery runs
 * and releases name it. A discovery run is frozen once
 * its sources have answered; freezing is what turns observations into candidates,
 * so a run where a required source fell short freezes only when the steward
 * accepts the gap explicitly.
 */
export function CollectionSetup({ onChanged }: { onChanged?: () => void }) {
  const [scopes, setScopes] = useState<ScopeVersion[]>([]);
  const [runs, setRuns] = useState<DiscoveryRun[]>([]);
  const [gates, setGates] = useState<Record<string, ScopeGateReport>>({});
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<ScopeVersion | null>(null);
  /** What is open under each scope row: its read-only view, its rules, its history. */
  const [open, setOpen] = useState<{ id: string; what: "view" | "rules" | "history" } | null>(null);

  function toggle(scope: ScopeVersion, what: "view" | "rules" | "history") {
    setOpen((current) => (current?.id === scope.id && current.what === what ? null : { id: scope.id, what }));
  }

  const refresh = useCallback(async () => {
    try {
      const [nextScopes, nextRuns] = await Promise.all([listScopes(), listDiscoveryRuns()]);
      setScopes([...nextScopes].sort((a, b) => a.name.localeCompare(b.name) || b.version - a.version));
      setRuns(nextRuns);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "collection setup could not be loaded");
    }
  }, []);

  const live = useLiveRefresh(refresh, { active: runs.some((run) => SEARCHING.has(run.status)) });

  /** Run a change, reload, then report, so a refusal survives the reload. */
  async function act(action: () => Promise<unknown>, done: string) {
    setNotice(null);
    let failure: string | null = null;
    try {
      await action();
      onChanged?.();
    } catch (caught) {
      failure = caught instanceof Error ? caught.message : "the change was not made";
    }
    // Keep following: a started run progresses, and a frozen one feeds candidates.
    live.kick();
    await live.refresh();
    if (failure) setError(failure);
    else setNotice(done);
  }

  /** The next version number of each scope, for "Save as new version". */
  const latest = new Map<string, number>();
  for (const scope of scopes) {
    latest.set(scope.scope_id, Math.max(latest.get(scope.scope_id) ?? 0, scope.version));
  }

  function remove(scope: ScopeVersion) {
    if (
      window.confirm(
        `Delete “${scope.name}” v${scope.version}? It is unapproved, so nothing has used it; ` +
          "the deletion is recorded in the audit log and cannot be undone.",
      )
    ) {
      if (editing?.id === scope.id) setEditing(null);
      void act(() => deleteScopeDraft(scope.id), `“${scope.name}” v${scope.version} deleted.`);
    }
  }

  async function gate(scope: ScopeVersion) {
    try {
      const report = await checkScopeGate(scope.id);
      setGates((current) => ({ ...current, [scope.id]: report }));
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "the gate could not be checked");
    }
  }

  return (
    <div className="space-y-4">
      {error ? (
        <Alert tone="danger" role="alert">
          {error}
        </Alert>
      ) : null}
      {notice ? (
        <Alert tone="success" role="status">
          {notice}
        </Alert>
      ) : null}

      <Panel
        title="Scopes"
        flush
        aside={
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setEditing(null);
              setCreating((value) => !(value || editing));
            }}
          >
            {creating || editing ? "Cancel" : "New scope"}
          </Button>
        }
      >
        {creating || editing ? (
          <ScopeForm
            key={editing?.id ?? "new"}
            initial={editing}
            mode={!editing ? "create" : editing.approval_state === "draft" || editing.approval_state === "in_review" ? "edit" : "next"}
            nextVersion={editing ? (latest.get(editing.scope_id) ?? editing.version) + 1 : 1}
            onSaved={async (message) => {
              setCreating(false);
              setEditing(null);
              setGates({});
              setNotice(`${message} Check its gate, then approve it.`);
              await live.refresh();
            }}
            onError={setError}
          />
        ) : null}
        {scopes.length === 0 ? (
          <div className="px-5 py-3">
            <Empty>No scope yet. A scope says what the collection is for and where to search.</Empty>
          </div>
        ) : (
          <ul className={rowList}>
            {scopes.map((scope) => {
              const report = gates[scope.id];
              const approved = scope.approval_state === "approved";
              const editable = !approved && scope.approval_state !== "retired";
              const shown = open?.id === scope.id ? open.what : null;
              return (
                <li key={scope.id} className={row}>
                  <div className="flex flex-wrap items-center gap-2">
                    <strong className="text-sm text-heading">{scope.name}</strong>
                    <span className="font-mono text-xs text-muted">v{scope.version}</span>
                    <StateChip state={scope.approval_state} kind="approval" />
                    <span className="text-xs text-muted">
                      {scope.connectors.join(", ")}
                      {scope.frozen_at ? ` · frozen ${new Date(scope.frozen_at).toLocaleString()}` : ""}
                    </span>
                    <div className="ml-auto flex flex-wrap gap-2">
                      {!editable ? (
                        <Button
                          variant="ghost"
                          size="sm"
                          aria-pressed={shown === "view"}
                          aria-label={`View ${scope.name} v${scope.version}`}
                          onClick={() => toggle(scope, "view")}
                        >
                          View
                        </Button>
                      ) : null}
                      {!editable ? (
                        <Button
                          variant="ghost"
                          size="sm"
                          aria-pressed={shown === "history"}
                          aria-label={`History of ${scope.name} v${scope.version}`}
                          onClick={() => toggle(scope, "history")}
                        >
                          History
                        </Button>
                      ) : null}
                      {!editable ? (
                        <Button
                          variant="ghost"
                          size="sm"
                          aria-label={`New version of ${scope.name} from v${scope.version}`}
                          onClick={() => {
                            setCreating(false);
                            setOpen(null);
                            setEditing(scope);
                            setNotice(null);
                          }}
                        >
                          New version
                        </Button>
                      ) : null}
                      {approved ? (
                        <Button
                          variant="ghost"
                          size="sm"
                          aria-pressed={shown === "rules"}
                          aria-label={`Apply the rules of ${scope.name} v${scope.version}`}
                          onClick={() => toggle(scope, "rules")}
                        >
                          Apply rules
                        </Button>
                      ) : null}
                      {editable ? (
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => {
                            setCreating(false);
                            setEditing(scope);
                            setNotice(null);
                          }}
                        >
                          Edit
                        </Button>
                      ) : null}
                      {editable ? (
                        <Button variant="danger" size="sm" onClick={() => remove(scope)}>
                          Delete
                        </Button>
                      ) : null}
                      <Button variant="ghost" size="sm" onClick={() => gate(scope)}>
                        Check gate
                      </Button>
                      {approved ? (
                        <Button
                          size="sm"
                          onClick={() =>
                            act(
                              () => startDiscovery(scope.id),
                              `Discovery started for “${scope.name}”. Its sources are being searched.`,
                            )
                          }
                        >
                          Start discovery
                        </Button>
                      ) : scope.approval_state !== "retired" ? (
                        <Button
                          size="sm"
                          onClick={() =>
                            act(() => approveScope(scope.id), `“${scope.name}” v${scope.version} is approved.`)
                          }
                        >
                          Approve
                        </Button>
                      ) : null}
                    </div>
                  </div>
                  {shown === "view" ? (
                    <ScopeForm initial={scope} mode="view" nextVersion={scope.version} onSaved={() => undefined} onError={setError} />
                  ) : null}
                  {shown === "rules" ? <ScopeRules scope={scope} onChanged={onChanged} /> : null}
                  {shown === "history" ? <ReviewHistory scope={scope} /> : null}
                  {report ? (
                    <div className="mt-2">
                      {report.passed ? (
                        <Alert tone="success" role="status">
                          Gate passed: {report.compiled_connectors.length} source
                          {report.compiled_connectors.length === 1 ? "" : "s"} compiled
                          {report.estimated_requests !== null
                            ? `, about ${report.estimated_requests} requests`
                            : ""}
                          .
                        </Alert>
                      ) : (
                        <Alert tone="warning" role="status">
                          Gate not passed: {report.blockers.join("; ")}
                          {editable ? ". Select Edit to fix it." : ""}
                        </Alert>
                      )}
                    </div>
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}
      </Panel>

      <Panel title="Discovery runs" flush aside={<LiveStatus {...live} />}>
        {runs.length === 0 ? (
          <div className="px-5 py-3">
            <Empty>No discovery run yet. Start one from an approved scope.</Empty>
          </div>
        ) : (
          <ul className={rowList}>
            {runs.map((run) => (
              <li key={run.id} className={row}>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-xs">{run.id.slice(0, 8)}</span>
                  <StateChip state={run.status} kind="discovery_run" />
                  <span className="text-xs text-muted">
                    {run.started_at ? `started ${new Date(run.started_at).toLocaleString()}` : "not started"}
                    {run.frozen_at ? ` · frozen ${new Date(run.frozen_at).toLocaleString()}` : ""}
                  </span>
                  {runDeletable(run) ? (
                    <div className="ml-auto">
                      <DeleteButton
                        what={`discovery run ${run.id.slice(0, 8)}`}
                        consequence="The pages it fetched are removed, and the next run of the same search starts again from where this one started."
                        onDelete={() =>
                          void act(() => deleteDiscoveryRun(run.id), `Discovery run ${run.id.slice(0, 8)} deleted.`)
                        }
                      />
                    </div>
                  ) : null}
                  {FREEZABLE.has(run.status) ? (
                    <div className={runDeletable(run) ? "" : "ml-auto"}>
                      {run.status === "degraded" ? (
                        <Button
                          variant="danger"
                          size="sm"
                          onClick={() => {
                            if (
                              window.confirm(
                                "A required source did not finish. Freeze anyway and accept the gap? The release records that it was accepted.",
                              )
                            ) {
                              void act(() => freezeDiscovery(run.id, true), "Run frozen with the gap accepted.");
                            }
                          }}
                        >
                          Freeze, accepting the gap
                        </Button>
                      ) : (
                        <Button
                          size="sm"
                          onClick={() => act(() => freezeDiscovery(run.id), "Run frozen. Its candidates are below.")}
                        >
                          Freeze
                        </Button>
                      )}
                    </div>
                  ) : null}
                </div>
                {run.degraded_reason ? (
                  <p className="mt-1 text-xs text-warning-strong">{run.degraded_reason}</p>
                ) : null}
                {run.connectors.length > 0 ? <SourceTable connectors={run.connectors} /> : null}
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}

/**
 * A new scope, or an unapproved version opened for editing. An edit is saved
 * either in place (the same version, corrected) or as the scope's next version,
 * leaving the one opened unchanged.
 */
/** One run's sources: what each returned, sortable like every table. */
function SourceTable({ connectors }: { connectors: DiscoveryRun["connectors"] }) {
  const table = useTable(connectors, {
    columns: {
      source: (state) => state.connector_id,
      outcome: (state) => state.outcome ?? "pending",
      seen: (state) => state.records_seen,
      declared: (state) => state.declared_total,
    },
  });
  return (
    <table className="data-table mt-2">
      <thead>
        <tr>
          <SortHeader table={table} column="source">
            Source
          </SortHeader>
          <SortHeader table={table} column="outcome">
            Outcome
          </SortHeader>
          <SortHeader table={table} column="seen" numeric>
            Seen
          </SortHeader>
          <SortHeader table={table} column="declared" numeric>
            Declared
          </SortHeader>
        </tr>
      </thead>
      <tbody>
        {table.rows.map((state) => (
          <tr key={state.connector_id}>
            <td className="font-mono text-xs">
              {state.connector_id}
              {state.required ? "" : " (optional)"}
            </td>
            <td>
              <StateChip state={state.outcome ?? "pending"} kind="source_outcome" />
            </td>
            <td className="numeric">{state.records_seen}</td>
            <td className="numeric">{state.declared_total ?? "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * A scope's document, in one of four modes:
 *
 * - `create`: a new scope.
 * - `edit`: an unapproved version, saved in place or as the next version.
 * - `next`: an approved or retired version opened as the basis of the next one;
 *   it can only be saved as a new draft, and the version opened stays as it is.
 * - `view`: read-only. An approved version is frozen: discovery runs, releases and
 *   decisions name it, so it is shown and never changed.
 */
function ScopeForm({
  initial,
  mode,
  nextVersion,
  onSaved,
  onError,
}: {
  initial?: ScopeVersion | null;
  mode: "create" | "edit" | "next" | "view";
  nextVersion: number;
  onSaved: (message: string) => void | Promise<void>;
  onError: (message: string) => void;
}) {
  const readOnly = mode === "view";
  const [name, setName] = useState(initial?.name ?? "");
  const [objective, setObjective] = useState(initial?.scientific_objective ?? "");
  const [inclusion, setInclusion] = useState((initial?.inclusion_criteria ?? []).join("\n"));
  const [exclusion, setExclusion] = useState((initial?.exclusion_criteria ?? []).join("\n"));
  const [concepts, setConcepts] = useState((initial?.concepts ?? []).join("\n"));
  const [exclusionTerms, setExclusionTerms] = useState((initial?.exclusion_terms ?? []).join("\n"));
  const [includeAt, setIncludeAt] = useState(
    String(Math.round((initial?.relevance_policy?.include_threshold ?? 0.75) * 100)),
  );
  const [excludeAt, setExcludeAt] = useState(
    String(Math.round((initial?.relevance_policy?.exclude_threshold ?? 0.25) * 100)),
  );
  const [uses, setUses] = useState((initial?.intended_uses ?? []).join("\n"));
  const [queries, setQueries] = useState<Record<string, string>>(() =>
    Object.fromEntries((initial?.connector_plan ?? []).map((entry) => [entry.connector_id, entry.query])),
  );
  const [busy, setBusy] = useState(false);

  const chosen = CONNECTORS.filter((connector) => connector.id in queries);

  /** "in-place" corrects the opened version; "new" saves the next version. */
  async function save(target: "in-place" | "new") {
    setBusy(true);
    try {
      const fields = {
        name: name.trim(),
        scientific_objective: objective.trim(),
        inclusion_criteria: lines(inclusion),
        exclusion_criteria: lines(exclusion),
        concepts: lines(concepts),
        exclusion_terms: lines(exclusionTerms),
        include_threshold: Number(includeAt) / 100,
        exclude_threshold: Number(excludeAt) / 100,
        intended_uses: lines(uses),
        connectors: chosen.map((connector) => ({
          connector_id: connector.id,
          query: (queries[connector.id] ?? "").trim(),
          required:
            initial?.connector_plan.find((entry) => entry.connector_id === connector.id)?.required ??
            true,
        })),
      };
      if (initial && target === "in-place") {
        await updateScopeDraft(initial.id, fields);
        await onSaved(`“${fields.name}” v${initial.version} corrected.`);
      } else {
        const saved = await createScope(initial ? { ...fields, scope_id: initial.scope_id } : fields);
        await onSaved(`Scope “${fields.name}” v${saved.version} saved as a draft.`);
      }
    } catch (caught) {
      onError(caught instanceof Error ? caught.message : "the scope was not saved");
    } finally {
      setBusy(false);
    }
  }

  const thresholdsValid =
    includeAt.trim() !== "" &&
    excludeAt.trim() !== "" &&
    Number(excludeAt) >= 0 &&
    Number(includeAt) <= 100 &&
    Number(excludeAt) < Number(includeAt);
  const complete =
    name.trim() &&
    objective.trim() &&
    thresholdsValid &&
    chosen.length > 0 &&
    chosen.every((connector) => (queries[connector.id] ?? "").trim());

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        if (!readOnly) void save(mode === "edit" ? "in-place" : "new");
      }}
      aria-label={
        readOnly
          ? `${initial?.name} v${initial?.version}, read-only`
          : initial
            ? `${mode === "next" ? "New version of" : "Edit"} ${initial.name}`
            : "New scope"
      }
      className={
        readOnly
          ? "mt-2 space-y-3 rounded-control border border-border bg-surface-subtle px-4 py-3"
          : "space-y-3 border-b border-border bg-surface-subtle px-5 py-4"
      }
    >
      {readOnly && initial ? (
        <p className="text-sm text-muted">
          v{initial.version} is {initial.approval_state === "approved" ? "approved and frozen" : initial.approval_state}
          {initial.frozen_at ? ` since ${new Date(initial.frozen_at).toLocaleString()}` : ""}: nothing can change
          it. To change its rules, select <strong>New version</strong>; this one stays as it is.
        </p>
      ) : mode === "next" && initial ? (
        <p className="text-sm text-muted">
          Starting from v{initial.version}, which stays frozen. <strong>Save as v{nextVersion}</strong> adds a
          draft; approve it, then use <strong>Apply rules</strong> to re-decide the candidates with it.
        </p>
      ) : initial ? (
        <p className="text-sm text-muted">
          Editing v{initial.version}. <strong>Save changes</strong> corrects v{initial.version}
          itself; <strong>Save as v{nextVersion}</strong> keeps v{initial.version} unchanged and adds
          a new draft.
        </p>
      ) : null}
      <fieldset disabled={readOnly} className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Name">
            <Input required value={name} onChange={(event) => setName(event.target.value)} />
          </Field>
          <Field label="Scientific objective">
            <Input required value={objective} onChange={(event) => setObjective(event.target.value)} />
          </Field>
          <Field label="Inclusion criteria (one per line)">
            <textarea
              rows={3}
              className={textareaClass}
              value={inclusion}
              onChange={(event) => setInclusion(event.target.value)}
            />
          </Field>
          <Field label="Exclusion criteria (one per line)">
            <textarea
              rows={3}
              className={textareaClass}
              value={exclusion}
              onChange={(event) => setExclusion(event.target.value)}
            />
          </Field>
          <Field label="Intended uses (one per line)">
            <textarea
              rows={2}
              className={textareaClass}
              placeholder="literature search"
              value={uses}
              onChange={(event) => setUses(event.target.value)}
            />
          </Field>
        </div>
        <fieldset className="space-y-2 rounded-control border border-border px-3 py-2">
          <legend className="px-1 text-sm font-medium text-ink">Screening rules</legend>
          <p className="text-xs text-muted">
            What decides a candidate automatically. The criteria above are for people; these are
            matched against each paper&apos;s title, abstract and subject terms.
          </p>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Concepts (one per line; alternatives separated by |)">
              <textarea
                rows={3}
                className={textareaClass}
                placeholder={"yarrowia | y. lipolytica\nlipid | oleaginous"}
                value={concepts}
                onChange={(event) => setConcepts(event.target.value)}
              />
            </Field>
            <Field label="Exclusion terms (one per line)">
              <textarea
                rows={3}
                className={textareaClass}
                placeholder={"plant\nmammalian cell"}
                value={exclusionTerms}
                onChange={(event) => setExclusionTerms(event.target.value)}
              />
            </Field>
            <Field label="Include when at least this % of concepts appear">
              <Input
                type="number"
                min={0}
                max={100}
                value={includeAt}
                onChange={(event) => setIncludeAt(event.target.value)}
              />
            </Field>
            <Field label="Exclude when at most this % appear (papers with an abstract)">
              <Input
                type="number"
                min={0}
                max={100}
                value={excludeAt}
                onChange={(event) => setExcludeAt(event.target.value)}
              />
            </Field>
          </div>
          {!thresholdsValid ? (
            <p className="text-xs text-danger-strong" role="alert">
              The exclude percentage must be below the include percentage.
            </p>
          ) : null}
        </fieldset>
        {readOnly ? null : (
          <QueryHelper
            onQueries={(written) =>
              setQueries(
                Object.fromEntries(
                  CONNECTORS.filter((connector) => connector.id in written).map((connector) => [
                    connector.id,
                    written[connector.id] ?? "",
                  ]),
                ),
              )
            }
          />
        )}
        <fieldset className="space-y-2">
          <legend className="mb-1 text-sm font-medium text-ink">
            Sources, each with a query in its own syntax
          </legend>
          {CONNECTORS.filter((connector) => !readOnly || connector.id in queries).map((connector) => {
            const on = connector.id in queries;
            return (
              <div key={connector.id} className="grid items-center gap-2 sm:grid-cols-[9rem_1fr]">
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={on}
                    onChange={() =>
                      setQueries((current) => {
                        const next = { ...current };
                        if (on) delete next[connector.id];
                        else next[connector.id] = "";
                        return next;
                      })
                    }
                  />
                  {connector.label}
                </label>
                <Input
                  aria-label={`${connector.label} query`}
                  disabled={!on}
                  placeholder={on ? connector.example : ""}
                  value={queries[connector.id] ?? ""}
                  onChange={(event) =>
                    setQueries((current) => ({ ...current, [connector.id]: event.target.value }))
                  }
                />
              </div>
            );
          })}
        </fieldset>
      </fieldset>
      {readOnly ? null : (
        <div className="flex flex-wrap gap-2">
          <Button type="submit" disabled={busy || !complete}>
            {mode === "edit" ? "Save changes" : mode === "next" ? `Save as v${nextVersion}` : "Save as draft"}
          </Button>
          {mode === "edit" ? (
            <Button
              type="button"
              variant="ghost"
              disabled={busy || !complete}
              onClick={() => void save("new")}
            >
              Save as v{nextVersion}
            </Button>
          ) : null}
        </div>
      )}
    </form>
  );
}
