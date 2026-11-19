"use client";

import { useCallback, useEffect, useState } from "react";

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
  freezeDiscovery,
  listDiscoveryRuns,
  listScopes,
  startDiscovery,
} from "@/lib/api";
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

/**
 * Setting a collection up: its scope, and the discovery runs that search it.
 *
 * A scope version is immutable: editing one creates the next version. It has to
 * pass its gate (an objective, at least one compiled source query) and be
 * approved before discovery may run against it. A discovery run is frozen once
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

  useEffect(() => {
    void refresh();
  }, [refresh]);

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
    await refresh();
    if (failure) setError(failure);
    else setNotice(done);
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
          <Button variant="ghost" size="sm" onClick={() => setCreating((value) => !value)}>
            {creating ? "Cancel" : "New scope"}
          </Button>
        }
      >
        {creating ? (
          <ScopeForm
            onCreated={async (name) => {
              setCreating(false);
              setNotice(`Scope “${name}” saved as a draft. Check its gate, then approve it.`);
              await refresh();
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
              return (
                <li key={scope.id} className={row}>
                  <div className="flex flex-wrap items-center gap-2">
                    <strong className="text-sm text-heading">{scope.name}</strong>
                    <span className="font-mono text-xs text-muted">v{scope.version}</span>
                    <StateChip state={scope.approval_state} />
                    <span className="text-xs text-muted">{scope.connectors.join(", ")}</span>
                    <div className="ml-auto flex gap-2">
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

      <Panel title="Discovery runs" flush>
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
                  <StateChip state={run.status} />
                  <span className="text-xs text-muted">
                    {run.started_at ? `started ${new Date(run.started_at).toLocaleString()}` : "not started"}
                    {run.frozen_at ? ` · frozen ${new Date(run.frozen_at).toLocaleString()}` : ""}
                  </span>
                  {FREEZABLE.has(run.status) ? (
                    <div className="ml-auto">
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
                {run.connectors.length > 0 ? (
                  <table className="data-table mt-2">
                    <thead>
                      <tr>
                        <th>Source</th>
                        <th>Outcome</th>
                        <th className="numeric">Seen</th>
                        <th className="numeric">Declared</th>
                      </tr>
                    </thead>
                    <tbody>
                      {run.connectors.map((state) => (
                        <tr key={state.connector_id}>
                          <td className="font-mono text-xs">
                            {state.connector_id}
                            {state.required ? "" : " (optional)"}
                          </td>
                          <td>
                            <StateChip state={state.outcome ?? "pending"} />
                          </td>
                          <td className="numeric">{state.records_seen}</td>
                          <td className="numeric">{state.declared_total ?? "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}

function ScopeForm({
  onCreated,
  onError,
}: {
  onCreated: (name: string) => void | Promise<void>;
  onError: (message: string) => void;
}) {
  const [name, setName] = useState("");
  const [objective, setObjective] = useState("");
  const [inclusion, setInclusion] = useState("");
  const [exclusion, setExclusion] = useState("");
  const [queries, setQueries] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);

  const chosen = CONNECTORS.filter((connector) => connector.id in queries);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      await createScope({
        name: name.trim(),
        scientific_objective: objective.trim(),
        inclusion_criteria: lines(inclusion),
        exclusion_criteria: lines(exclusion),
        connectors: chosen.map((connector) => ({
          connector_id: connector.id,
          query: (queries[connector.id] ?? "").trim(),
        })),
      });
      await onCreated(name.trim());
    } catch (caught) {
      onError(caught instanceof Error ? caught.message : "the scope was not saved");
    } finally {
      setBusy(false);
    }
  }

  const complete =
    name.trim() &&
    objective.trim() &&
    chosen.length > 0 &&
    chosen.every((connector) => (queries[connector.id] ?? "").trim());

  return (
    <form onSubmit={submit} className="space-y-3 border-b border-border bg-surface-subtle px-5 py-4">
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
      </div>
      <fieldset className="space-y-2">
        <legend className="mb-1 text-sm font-medium text-ink">
          Sources, each with a query in its own syntax
        </legend>
        {CONNECTORS.map((connector) => {
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
      <Button type="submit" disabled={busy || !complete}>
        Save as draft
      </Button>
    </form>
  );
}
