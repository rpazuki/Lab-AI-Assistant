"use client";

import { useState } from "react";

import { Alert, Button, Field, Input, Panel } from "@/components/ui/primitives";

/** One question of a benchmark as the editor holds it: relevant papers one per line. */
interface CaseDraft {
  case_id: string;
  question: string;
  relevant: string;
  notes: string;
}

export interface BenchmarkDefinition {
  benchmark_id: string;
  version: string;
  kind: string;
  description: string;
  origin: string;
  licence: string;
  permitted_use: string;
  sensitivity?: string;
  blinded?: boolean;
  cases?: { case_id: string; question?: string | null; relevant?: string[]; notes?: string | null }[];
  probes?: unknown[];
  thresholds?: Record<string, number>;
  [extra: string]: unknown;
}

/** The next patch version: a correction to a gold label is a new version, never an edit. */
export function nextVersion(version: string): string {
  const parts = version.split(".");
  const last = Number(parts.at(-1));
  if (parts.length >= 2 && Number.isInteger(last)) {
    return [...parts.slice(0, -1), String(last + 1)].join(".");
  }
  return `${version}.1`;
}

export const EMPTY_BENCHMARK: BenchmarkDefinition = {
  benchmark_id: "",
  version: "1.0.0",
  kind: "retrieval",
  description: "",
  origin: "written by the lab",
  licence: "internal",
  permitted_use: "internal evaluation",
  sensitivity: "internal",
  cases: [{ case_id: "q1", question: "", relevant: [] }],
  thresholds: { "recall@5": 0.6 },
};

const textareaClass =
  "w-full rounded-control border border-border-strong bg-surface px-3 py-2 text-sm text-ink shadow-control focus:border-primary";
const selectClass =
  "w-full rounded-control border border-border-strong bg-surface px-3 py-2 text-sm text-ink shadow-control";

function toDrafts(definition: BenchmarkDefinition): CaseDraft[] {
  return (definition.cases ?? []).map((item) => ({
    case_id: item.case_id,
    question: item.question ?? "",
    relevant: (item.relevant ?? []).join("\n"),
    notes: item.notes ?? "",
  }));
}

/**
 * Edit a benchmark's questions and thresholds, and register the result as a new
 * version — the predecessor's question-set editor, on this system's rule that a
 * registered version never changes. Opening an existing benchmark proposes the
 * next patch version, so a corrected gold label is recorded as the correction it
 * is. Policy probes are kept as written; they are edited in the file.
 */
export function BenchmarkEditor({
  initial,
  editing,
  onSave,
  onCancel,
}: {
  initial: BenchmarkDefinition;
  /** True when this starts from a registered version rather than from nothing. */
  editing: boolean;
  onSave: (definition: BenchmarkDefinition) => Promise<void>;
  onCancel: () => void;
}) {
  const [benchmarkId, setBenchmarkId] = useState(initial.benchmark_id);
  const [version, setVersion] = useState(editing ? nextVersion(initial.version) : initial.version);
  const [kind, setKind] = useState(initial.kind);
  const [description, setDescription] = useState(initial.description);
  const [sensitivity, setSensitivity] = useState(initial.sensitivity ?? "internal");
  const [cases, setCases] = useState<CaseDraft[]>(toDrafts(initial));
  const [thresholds, setThresholds] = useState<[string, string][]>(
    Object.entries(initial.thresholds ?? {}).map(([name, value]) => [name, String(value)]),
  );
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const updateCase = (index: number, patch: Partial<CaseDraft>) =>
    setCases((current) => current.map((item, i) => (i === index ? { ...item, ...patch } : item)));

  function build(): BenchmarkDefinition | string {
    if (!benchmarkId.trim()) return "Give the benchmark an id.";
    if (editing && version === initial.version) {
      return `Version ${version} is already registered; a change needs a new version.`;
    }
    const ids = cases.map((item) => item.case_id.trim());
    if (ids.some((id) => !id)) return "Every question needs a case id.";
    if (new Set(ids).size !== ids.length) return "Case ids must be unique.";
    if (kind !== "policy" && cases.some((item) => !item.question.trim())) {
      return "Every case needs a question.";
    }
    const parsed: Record<string, number> = {};
    for (const [name, value] of thresholds) {
      if (!name.trim()) continue;
      const number = Number(value);
      if (!Number.isFinite(number)) return `Threshold ${name} is not a number.`;
      parsed[name.trim()] = number;
    }
    return {
      ...initial,
      benchmark_id: benchmarkId.trim(),
      version: version.trim(),
      kind,
      description: description.trim(),
      sensitivity,
      cases: cases.map((item) => ({
        case_id: item.case_id.trim(),
        question: item.question.trim() || null,
        relevant: item.relevant
          .split("\n")
          .map((line) => line.trim())
          .filter(Boolean),
        ...(item.notes.trim() ? { notes: item.notes.trim() } : {}),
      })),
      thresholds: parsed,
    };
  }

  async function save() {
    const definition = build();
    if (typeof definition === "string") {
      setProblem(definition);
      return;
    }
    setProblem(null);
    setBusy(true);
    try {
      await onSave(definition);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel
      title={editing ? `New version of ${initial.benchmark_id}` : "New benchmark"}
      aside={
        <Button variant="ghost" size="sm" onClick={onCancel}>
          Cancel
        </Button>
      }
    >
      <div className="space-y-4">
        {initial.blinded ? (
          <Alert tone="warning" role="status">
            This benchmark is blinded: its gold answers are shown here. Edit it away from anyone
            whose answers it measures.
          </Alert>
        ) : null}
        <div className="grid gap-3 sm:grid-cols-4">
          <Field label="Benchmark id">
            <Input value={benchmarkId} disabled={editing} onChange={(event) => setBenchmarkId(event.target.value)} />
          </Field>
          <Field label="Version">
            <Input value={version} onChange={(event) => setVersion(event.target.value)} />
          </Field>
          <Field label="Kind">
            <select className={selectClass} value={kind} disabled={editing} onChange={(event) => setKind(event.target.value)}>
              <option value="retrieval">retrieval</option>
              <option value="generation">generation</option>
              <option value="policy">policy</option>
            </select>
          </Field>
          <Field label="Sensitivity">
            <select className={selectClass} value={sensitivity} onChange={(event) => setSensitivity(event.target.value)}>
              <option value="internal">internal</option>
              <option value="restricted">restricted</option>
            </select>
          </Field>
        </div>
        <Field label="Description">
          <Input value={description} onChange={(event) => setDescription(event.target.value)} />
        </Field>

        <fieldset className="space-y-3">
          <legend className="text-sm font-medium text-ink">Questions ({cases.length})</legend>
          {cases.map((item, index) => (
            <div key={index} className="space-y-2 rounded-control border border-border bg-surface-subtle p-3">
              <div className="grid gap-2 sm:grid-cols-[10rem_1fr_auto] sm:items-end">
                <Field label="Case id">
                  <Input value={item.case_id} onChange={(event) => updateCase(index, { case_id: event.target.value })} />
                </Field>
                <Field label="Question">
                  <Input value={item.question} onChange={(event) => updateCase(index, { question: event.target.value })} />
                </Field>
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={`Remove ${item.case_id || `question ${index + 1}`}`}
                  onClick={() => setCases((current) => current.filter((_, i) => i !== index))}
                >
                  Remove
                </Button>
              </div>
              {kind === "retrieval" ? (
                <Field label="Relevant papers (doi:…, pmid:…, pmcid:… or a work id, one per line)">
                  <textarea
                    rows={2}
                    className={`${textareaClass} font-mono text-xs`}
                    value={item.relevant}
                    onChange={(event) => updateCase(index, { relevant: event.target.value })}
                  />
                </Field>
              ) : null}
              <Field label="Notes (optional)">
                <Input value={item.notes} onChange={(event) => updateCase(index, { notes: event.target.value })} />
              </Field>
            </div>
          ))}
          <Button
            variant="ghost"
            size="sm"
            onClick={() =>
              setCases((current) => [
                ...current,
                { case_id: `q${current.length + 1}`, question: "", relevant: "", notes: "" },
              ])
            }
          >
            Add a question
          </Button>
        </fieldset>

        <fieldset className="space-y-2">
          <legend className="text-sm font-medium text-ink">Thresholds</legend>
          {thresholds.map(([name, value], index) => (
            <div key={index} className="flex flex-wrap items-center gap-2">
              <Input
                aria-label={`Threshold ${index + 1} metric`}
                className="w-48"
                value={name}
                onChange={(event) =>
                  setThresholds((current) => current.map((pair, i) => (i === index ? [event.target.value, pair[1]] : pair)))
                }
              />
              <span className="text-sm text-muted">≥</span>
              <Input
                aria-label={`Threshold ${index + 1} value`}
                className="w-24"
                value={value}
                onChange={(event) =>
                  setThresholds((current) => current.map((pair, i) => (i === index ? [pair[0], event.target.value] : pair)))
                }
              />
              <Button variant="ghost" size="sm" onClick={() => setThresholds((current) => current.filter((_, i) => i !== index))}>
                Remove
              </Button>
            </div>
          ))}
          <Button variant="ghost" size="sm" onClick={() => setThresholds((current) => [...current, ["", ""]])}>
            Add a threshold
          </Button>
        </fieldset>

        {kind === "policy" ? (
          <p className="text-xs text-muted">
            Policy probes ({(initial.probes ?? []).length}) are kept as written: they name real
            accounts, so they are edited in the benchmark file.
          </p>
        ) : null}
        {problem ? (
          <Alert tone="danger" role="alert">
            {problem}
          </Alert>
        ) : null}
        <Button onClick={save} disabled={busy}>
          {busy ? "Registering…" : `Register ${benchmarkId || "benchmark"}@${version}`}
        </Button>
      </div>
    </Panel>
  );
}
