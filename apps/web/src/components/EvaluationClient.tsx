"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { Alert, Button, Empty, Field, Panel, StateChip, row, rowList } from "@/components/ui/primitives";
import {
  BenchmarkEditor,
  EMPTY_BENCHMARK,
  type BenchmarkDefinition,
} from "@/components/BenchmarkEditor";
import { AnswerReviewSection, RunComparisonPanel } from "@/components/EvaluationWorkbench";
import {
  ApiError,
  cancelEvaluationRun,
  getBenchmark,
  getEvaluationRun,
  listBenchmarks,
  listEvaluationRuns,
  listReleaseGates,
  listReleases,
  registerBenchmark,
  requestEvaluationRun,
  setReleaseGate,
} from "@/lib/api";
import type { Benchmark, EvaluationRun, Release, ReleaseGate } from "@/types";

/** How often the page asks again while a run is queued or running. */
const POLL_MS = 4000;

/** A starting point for a benchmark file: a retrieval set naming papers by DOI. */
export const EXAMPLE_BENCHMARK = {
  benchmark_id: "rla-retrieval",
  version: "1.0.0",
  kind: "retrieval",
  description: "Questions lab members ask, with the papers that answer them.",
  origin: "written by the lab",
  licence: "internal",
  permitted_use: "internal evaluation",
  sensitivity: "internal",
  cases: [
    {
      case_id: "yarrowia-lipid-titre",
      question: "Which Yarrowia lipolytica strains reached the highest lipid titre?",
      relevant: ["doi:10.1000/example-one", "pmid:12345678"],
    },
  ],
  thresholds: { "recall@5": 0.6, citation_ready_rate: 0.8 },
};

const select =
  "w-full rounded-control border border-border-strong bg-surface px-3 py-2 text-sm text-ink shadow-control focus:border-primary";

/**
 * Evaluation: register benchmark files, run one against a release, read the result.
 *
 * Which releases can be measured, which benchmarks are valid, and whether a run
 * passed are the API's decisions; this page shows them and polls while a run is
 * on the worker queue. A refusal arrives as the API's own explanation.
 */
export function EvaluationClient() {
  const [benchmarks, setBenchmarks] = useState<Benchmark[]>([]);
  const [releases, setReleases] = useState<Release[]>([]);
  const [runs, setRuns] = useState<EvaluationRun[]>([]);
  const [error, setError] = useState<{ message: string; problems: string[] } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [benchmarkKey, setBenchmarkKey] = useState("");
  const [releaseId, setReleaseId] = useState("");
  const [busy, setBusy] = useState(false);
  const [gates, setGates] = useState<ReleaseGate[]>([]);
  const [compare, setCompare] = useState<string[]>([]);
  const [comparing, setComparing] = useState<[string, string] | null>(null);

  const fail = useCallback((caught: unknown, fallback: string) => {
    const problems =
      caught instanceof ApiError && Array.isArray(caught.context.problems)
        ? (caught.context.problems as unknown[]).map(String)
        : [];
    setError({ message: caught instanceof Error ? caught.message : fallback, problems });
  }, []);

  const refresh = useCallback(async () => {
    try {
      const [benchmarkItems, releaseBody, runItems, gateBody] = await Promise.all([
        listBenchmarks(),
        listReleases(),
        listEvaluationRuns(),
        listReleaseGates().catch(() => ({ items: [] as ReleaseGate[] })),
      ]);
      setBenchmarks(benchmarkItems);
      setGates(gateBody.items);
      setReleases(releaseBody.items);
      setRuns(runItems);
      // Default to what is being served: the release most people will ask about.
      const served = releaseBody.aliases.find((alias) => alias.alias === "current")?.release_id;
      setReleaseId((current) => current || served || releaseBody.items[0]?.id || "");
      setBenchmarkKey((current) => current || benchmarkItems[0]?.key || "");
    } catch (caught) {
      fail(caught, "evaluation could not be loaded");
    }
  }, [fail]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const pending = runs.some((run) => run.status === "queued" || run.status === "running");
  useEffect(() => {
    if (!pending) return;
    const timer = window.setInterval(() => void refresh(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [pending, refresh]);

  const releaseLabel = useMemo(() => {
    const labels = new Map(
      releases.map((release) => [release.id, `${release.name}@${release.semantic_version}`]),
    );
    return (id: string | null) => (id ? (labels.get(id) ?? id.slice(0, 8)) : "—");
  }, [releases]);

  async function act(action: () => Promise<unknown>, done: string, fallback: string) {
    setNotice(null);
    try {
      await action();
      await refresh();
      setError(null);
      setNotice(done);
    } catch (caught) {
      fail(caught, fallback);
    }
  }

  function toggleCompare(runId: string) {
    setCompare((current) =>
      current.includes(runId)
        ? current.filter((id) => id !== runId)
        : [...current, runId].slice(-2),
    );
  }

  const describeRun = (runId: string) => {
    const run = runs.find((item) => item.id === runId);
    return run ? `${run.benchmark} on ${releaseLabel(run.release_id)}` : runId.slice(0, 8);
  };

  async function startRun() {
    const benchmark = benchmarks.find((item) => item.key === benchmarkKey);
    if (!benchmark || !releaseId) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await requestEvaluationRun({
        benchmark_id: benchmark.benchmark_id,
        version: benchmark.version,
        release_id: releaseId,
      });
      setNotice(`${benchmark.key} is queued against ${releaseLabel(releaseId)}.`);
      await refresh();
    } catch (caught) {
      fail(caught, "the run could not be queued");
    } finally {
      setBusy(false);
    }
  }

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
        <Alert tone="info" role="status">
          {notice}
        </Alert>
      ) : null}

      <Panel title="Run an evaluation">
        {benchmarks.length === 0 ? (
          <Empty>Register a benchmark below first.</Empty>
        ) : (
          <div className="grid gap-4 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
            <Field label="Benchmark">
              <select
                className={select}
                value={benchmarkKey}
                onChange={(event) => setBenchmarkKey(event.target.value)}
              >
                {benchmarks.map((benchmark) => (
                  <option key={benchmark.key} value={benchmark.key}>
                    {benchmark.key} ({benchmark.kind})
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Release">
              <select
                className={select}
                value={releaseId}
                onChange={(event) => setReleaseId(event.target.value)}
              >
                {releases.length === 0 ? <option value="">No releases yet</option> : null}
                {releases.map((release) => (
                  <option key={release.id} value={release.id}>
                    {release.name}@{release.semantic_version} ({release.status})
                  </option>
                ))}
              </select>
            </Field>
            <Button onClick={startRun} disabled={busy || !benchmarkKey || !releaseId}>
              {busy ? "Queuing…" : "Run"}
            </Button>
          </div>
        )}
        <p className="mt-3 text-xs text-muted">
          A release can be measured once it is approved and indexed. Runs execute on the
          evaluation worker as you, with your access; a generation run asks the model one
          question per case.
        </p>
      </Panel>

      <Panel
        title="Runs"
        flush
        aside={
          <Button
            variant="ghost"
            size="sm"
            disabled={compare.length !== 2}
            onClick={() => setComparing([compare[0]!, compare[1]!])}
          >
            Compare selected ({compare.length}/2)
          </Button>
        }
      >
        {runs.length === 0 ? (
          <div className="px-5 py-3">
            <Empty>No evaluation has run yet.</Empty>
          </div>
        ) : (
          <ul className={`${rowList} text-sm`}>
            {runs.map((run) => (
              <RunRow
                key={run.id}
                run={run}
                releaseLabel={releaseLabel(run.release_id)}
                selected={compare.includes(run.id)}
                onSelect={() => toggleCompare(run.id)}
                onCancel={() =>
                  act(
                    () => cancelEvaluationRun(run.id),
                    `${run.benchmark} is cancelled.`,
                    "the run could not be cancelled",
                  )
                }
              />
            ))}
          </ul>
        )}
      </Panel>

      {comparing ? (
        <RunComparisonPanel
          baseId={comparing[0]}
          candidateId={comparing[1]}
          describe={describeRun}
          onClose={() => setComparing(null)}
        />
      ) : null}

      <BenchmarksPanel
        benchmarks={benchmarks}
        gates={gates}
        onGate={(benchmark, require) =>
          act(
            () => setReleaseGate(benchmark.benchmark_id, require ? benchmark.version : null),
            require
              ? `Publishing a release now needs a passing run of ${benchmark.key}.`
              : `${benchmark.benchmark_id} is no longer required for publishing.`,
            "the requirement was not changed",
          )
        }
        onRegistered={async (message) => {
          setError(null);
          setNotice(message);
          await refresh();
        }}
        onError={(caught) => fail(caught, "the benchmark could not be registered")}
      />
    </div>
  );
}

function verdict(run: EvaluationRun): string {
  if (run.status !== "succeeded") return run.status;
  return run.passed ? "passed" : "failed gates";
}

function formatMetric(value: unknown): string {
  return typeof value === "number" && !Number.isInteger(value) ? value.toFixed(3) : String(value);
}

function RunRow({
  run,
  releaseLabel,
  selected,
  onSelect,
  onCancel,
}: {
  run: EvaluationRun;
  releaseLabel: string;
  selected: boolean;
  onSelect: () => void;
  onCancel: () => void;
}) {
  const [detail, setDetail] = useState<EvaluationRun | null>(null);
  const [open, setOpen] = useState(false);

  async function toggle() {
    const next = !open;
    setOpen(next);
    if (next) setDetail(await getEvaluationRun(run.id).catch(() => null));
  }

  return (
    <li className={row}>
      <div className="flex flex-wrap items-center gap-2">
        {run.status === "succeeded" ? (
          <input
            type="checkbox"
            aria-label={`Compare ${run.benchmark} on ${releaseLabel}`}
            checked={selected}
            onChange={onSelect}
          />
        ) : null}
        <strong className="text-heading">{run.benchmark}</strong>
        <span className="text-muted">on {releaseLabel}</span>
        <StateChip state={verdict(run)} />
        <span className="ml-auto text-xs text-muted">
          {run.created_at ? new Date(run.created_at).toLocaleString() : ""}
        </span>
        {run.status === "queued" || run.status === "running" ? (
          <Button variant="danger" size="sm" onClick={onCancel}>
            Cancel
          </Button>
        ) : null}
        <Button variant="ghost" size="sm" onClick={toggle} aria-expanded={open}>
          {open ? "Hide" : "Details"}
        </Button>
      </div>
      {Object.keys(run.metrics).length > 0 ? (
        <dl className="mt-2 flex flex-wrap gap-x-5 gap-y-1 font-mono text-xs">
          {Object.entries(run.metrics).map(([name, value]) => (
            <div key={name} className="flex gap-1.5">
              <dt className="text-muted">{name}</dt>
              <dd className="text-ink">{formatMetric(value)}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      {run.error_detail ? (
        <Alert tone="danger" className="mt-2 break-all font-mono text-xs">
          {run.error_detail}
        </Alert>
      ) : null}
      {open ? <RunDetail run={detail ?? run} /> : null}
      {open && run.kind === "generation" && run.status === "succeeded" ? (
        <div className="mt-3">
          <AnswerReviewSection runId={run.id} />
        </div>
      ) : null}
    </li>
  );
}

function RunDetail({ run }: { run: EvaluationRun }) {
  const cases = run.per_case ?? [];
  // Question and answer text are long; reviewers read them on the review page.
  const hidden = new Set(["retrieved", "question", "answer"]);
  const columns = Array.from(
    new Set(cases.flatMap((item) => Object.keys(item)).filter((key) => !hidden.has(key))),
  );
  return (
    <div className="mt-3 space-y-3">
      {Object.keys(run.gates).length > 0 ? (
        <ul className="flex flex-wrap gap-1">
          {Object.entries(run.gates).map(([gate, ok]) => (
            <li key={gate}>
              <StateChip state={`${gate}=${ok ? "pass" : "fail"}`} />
            </li>
          ))}
        </ul>
      ) : null}
      {run.failures.length > 0 ? (
        <ul className="list-disc pl-6 text-xs text-danger-strong">
          {run.failures.map((failure) => (
            <li key={failure}>{failure}</li>
          ))}
        </ul>
      ) : null}
      {cases.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full text-left font-mono text-xs">
            <thead className="text-muted">
              <tr>
                {columns.map((column) => (
                  <th key={column} className="border-b border-border px-2 py-1 font-medium">
                    {column}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {cases.map((item, index) => (
                <tr key={index} className="border-b border-border last:border-0">
                  {columns.map((column) => (
                    <td key={column} className="px-2 py-1 align-top text-ink">
                      {Array.isArray(item[column])
                        ? (item[column] as unknown[]).join(", ")
                        : item[column] === undefined
                          ? ""
                          : formatMetric(item[column])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      <p className="font-mono text-xs text-muted">
        benchmark checksum {run.benchmark_checksum.slice(0, 16)}
        {run.task_id ? ` · task ${run.task_id}` : ""}
      </p>
    </div>
  );
}

function BenchmarksPanel({
  benchmarks,
  gates,
  onGate,
  onRegistered,
  onError,
}: {
  benchmarks: Benchmark[];
  gates: ReleaseGate[];
  onGate: (benchmark: Benchmark, require: boolean) => void;
  onRegistered: (message: string) => Promise<void>;
  onError: (caught: unknown) => void;
}) {
  const [adding, setAdding] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [editor, setEditor] = useState<{ initial: BenchmarkDefinition; editing: boolean } | null>(
    null,
  );

  async function openEditor(benchmark: Benchmark) {
    try {
      const full = await getBenchmark(benchmark.benchmark_id, benchmark.version);
      setEditor({ initial: full.definition as BenchmarkDefinition, editing: true });
      setAdding(false);
    } catch (caught) {
      onError(caught);
    }
  }

  async function saveEdited(definition: BenchmarkDefinition) {
    try {
      const saved = await registerBenchmark(definition);
      setEditor(null);
      await onRegistered(
        saved.created
          ? `Registered ${saved.key}.`
          : `${saved.key} was already registered with this content; nothing changed.`,
      );
    } catch (caught) {
      onError(caught);
    }
  }

  async function readFile(file: File | undefined) {
    if (file) setText(await file.text());
  }

  async function register() {
    let definition: unknown;
    try {
      definition = JSON.parse(text);
    } catch {
      onError(new Error("The benchmark is not valid JSON."));
      return;
    }
    setBusy(true);
    try {
      const saved = await registerBenchmark(definition);
      setText("");
      setAdding(false);
      await onRegistered(
        saved.created
          ? `Registered ${saved.key}.`
          : `${saved.key} was already registered with this content; nothing changed.`,
      );
    } catch (caught) {
      onError(caught);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel
      title="Benchmarks"
      flush
      aside={
        <div className="flex gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setAdding(false);
              setEditor({ initial: EMPTY_BENCHMARK, editing: false });
            }}
          >
            Write one
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setAdding((value) => !value)}>
            {adding ? "Cancel" : "Add benchmark"}
          </Button>
        </div>
      }
    >
      {editor ? (
        <div className="border-b border-border p-5">
          <BenchmarkEditor
            initial={editor.initial}
            editing={editor.editing}
            onSave={saveEdited}
            onCancel={() => setEditor(null)}
          />
        </div>
      ) : null}
      {adding ? (
        <div className="space-y-3 border-b border-border p-5">
          <Field label="Benchmark file (JSON)">
            <input
              type="file"
              accept="application/json,.json"
              onChange={(event) => void readFile(event.target.files?.[0])}
              className="block text-sm text-ink file:mr-3 file:rounded-control file:border file:border-border-strong file:bg-surface file:px-2.5 file:py-1 file:text-xs"
            />
          </Field>
          <Field label="Or paste it">
            <textarea
              value={text}
              onChange={(event) => setText(event.target.value)}
              rows={10}
              spellCheck={false}
              className="w-full rounded-control border border-border-strong bg-surface px-3 py-2 font-mono text-xs text-ink shadow-control focus:border-primary"
            />
          </Field>
          <div className="flex flex-wrap items-center gap-2">
            <Button onClick={register} disabled={busy || !text.trim()}>
              {busy ? "Registering…" : "Register"}
            </Button>
            <Button
              variant="ghost"
              onClick={() => setText(JSON.stringify(EXAMPLE_BENCHMARK, null, 2))}
            >
              Start from an example
            </Button>
          </div>
          <p className="text-xs text-muted">
            <code>kind</code> is <code>retrieval</code>, <code>generation</code> or{" "}
            <code>policy</code>. Name relevant papers as <code>doi:…</code>,{" "}
            <code>pmid:…</code>, <code>pmcid:…</code> or a work id; one the corpus does not hold
            counts as a miss. A registered version cannot change: register a correction as a
            new <code>version</code>. Questions in a <code>restricted</code> benchmark are
            never sent to the model, so it runs retrieval and policy only.
          </p>
        </div>
      ) : null}
      {benchmarks.length === 0 ? (
        <div className="px-5 py-3">
          <Empty>No benchmark is registered.</Empty>
        </div>
      ) : (
        <ul className={`${rowList} text-sm`}>
          {benchmarks.map((benchmark) => (
            <li key={benchmark.key} className={row}>
              <div className="flex flex-wrap items-center gap-2">
                <strong className="text-heading">{benchmark.key}</strong>
                <StateChip state={benchmark.kind} />
                <StateChip state={benchmark.sensitivity} />
                {benchmark.blinded ? <StateChip state="blinded" /> : null}
                <span className="ml-auto font-mono text-xs text-muted">
                  {benchmark.cases + benchmark.probes} {benchmark.kind === "policy" ? "probes" : "cases"}
                  {" · "}
                  {benchmark.checksum.slice(0, 12)}
                </span>
                <Button variant="ghost" size="sm" onClick={() => openEditor(benchmark)}>
                  Edit as new version
                </Button>
                {gates.some((gate) => gate.key === benchmark.key) ? (
                  <>
                    <StateChip state="required_for_publishing" />
                    <Button variant="ghost" size="sm" onClick={() => onGate(benchmark, false)}>
                      Stop requiring
                    </Button>
                  </>
                ) : (
                  <Button variant="ghost" size="sm" onClick={() => onGate(benchmark, true)}>
                    Require for publishing
                  </Button>
                )}
              </div>
              {benchmark.description ? (
                <p className="mt-1 text-muted">{benchmark.description}</p>
              ) : null}
              {Object.keys(benchmark.thresholds).length > 0 ? (
                <p className="mt-1 font-mono text-xs text-ink">
                  thresholds:{" "}
                  {Object.entries(benchmark.thresholds)
                    .map(([name, value]) => `${name} ≥ ${value}`)
                    .join(", ")}
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
