import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EvaluationClient } from "@/components/EvaluationClient";
import { ApiError } from "@/lib/api";
import type { Benchmark, EvaluationRun } from "@/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listBenchmarks: vi.fn(),
    listReleases: vi.fn(),
    listEvaluationRuns: vi.fn(),
    registerBenchmark: vi.fn(),
    requestEvaluationRun: vi.fn(),
    getEvaluationRun: vi.fn(),
    getBenchmark: vi.fn(),
    listReleaseGates: vi.fn(async () => ({ items: [] })),
    setReleaseGate: vi.fn(async () => ({ items: [] })),
    cancelEvaluationRun: vi.fn(),
    compareEvaluationRuns: vi.fn(),
    listEvaluationReviewers: vi.fn(async () => [
      { user_id: "u7", email: "reviewer@lab.invalid", full_name: "Rev Iewer", role: "reviewer" },
    ]),
    listRunReviews: vi.fn(async () => ({
      run_id: "run-g",
      assigned: 2,
      submitted: 1,
      mean_scores: { correctness: 4, completeness: null },
      flag_counts: { corpus_gap: 1, hallucination: 0 },
      items: [],
    })),
    assignEvaluationReviews: vi.fn(async () => ({ items: [{ id: "a1" }] })),
  };
});

const api = await import("@/lib/api");

const BENCHMARK: Benchmark = {
  benchmark_id: "rla-retrieval",
  version: "1.0.0",
  key: "rla-retrieval@1.0.0",
  kind: "retrieval",
  description: "Lab questions",
  sensitivity: "internal",
  blinded: false,
  checksum: "abcdef0123456789",
  cases: 12,
  probes: 0,
  thresholds: { "recall@5": 0.6 },
  registered_by: "u1",
  created_at: "2026-11-19T10:00:00Z",
};

function run(overrides: Partial<EvaluationRun> = {}): EvaluationRun {
  return {
    id: "run-1",
    benchmark: "rla-retrieval@1.0.0",
    benchmark_checksum: "abcdef0123456789",
    release_id: "rel-2",
    kind: "retrieval",
    status: "succeeded",
    passed: false,
    metrics: { "recall@5": 0.41667, mrr: 0.5, cases: 12 },
    gates: { "retrieval:recall@5": false },
    failures: ["recall@5 measured 0.4167 below threshold 0.6000"],
    task_id: "t1",
    requested_by: "u1",
    error_detail: null,
    created_at: "2026-11-19T10:05:00Z",
    completed_at: "2026-11-19T10:06:00Z",
    ...overrides,
  };
}

const RELEASES = {
  items: [
    { id: "rel-1", name: "corpus", semantic_version: "1.0.0", status: "superseded" },
    { id: "rel-2", name: "corpus", semantic_version: "1.1.0", status: "published" },
  ],
  aliases: [{ alias: "current", release_id: "rel-2", previous_release_id: "rel-1" }],
};

describe("EvaluationClient", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listBenchmarks).mockResolvedValue([BENCHMARK]);
    vi.mocked(api.listReleases).mockResolvedValue(RELEASES as never);
    vi.mocked(api.listEvaluationRuns).mockResolvedValue([run()]);
  });

  it("shows each run's verdict, metrics and the release it measured", async () => {
    render(<EvaluationClient />);
    expect(await screen.findByText("failed gates")).toBeInTheDocument();
    expect(screen.getByText("on corpus@1.1.0")).toBeInTheDocument();
    expect(screen.getByText("0.417")).toBeInTheDocument();
  });

  it("queues the chosen benchmark against the served release by default", async () => {
    vi.mocked(api.requestEvaluationRun).mockResolvedValue(run({ status: "queued" }));
    render(<EvaluationClient />);
    await screen.findByText("failed gates");
    await userEvent.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() =>
      expect(api.requestEvaluationRun).toHaveBeenCalledWith({
        benchmark_id: "rla-retrieval",
        version: "1.0.0",
        release_id: "rel-2",
      }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent("queued against corpus@1.1.0");
  });

  it("opens a run's gates, failures and per-case results", async () => {
    vi.mocked(api.getEvaluationRun).mockResolvedValue(
      run({ per_case: [{ case_id: "q1", recall_at_k: 0, unresolved_relevant: ["pmid:1"] }] }),
    );
    render(<EvaluationClient />);
    await userEvent.click(await screen.findByRole("button", { name: "Details" }));
    expect(await screen.findByText("retrieval:recall@5=fail")).toBeInTheDocument();
    expect(screen.getByText("pmid:1")).toBeInTheDocument();
    expect(screen.getByText(/below threshold/)).toBeInTheDocument();
  });

  it("lists what is wrong with a rejected benchmark file", async () => {
    vi.mocked(api.registerBenchmark).mockRejectedValue(
      new ApiError({
        type: "about:blank",
        title: "schema violation",
        status: 422,
        detail: "the benchmark file is not valid",
        error_class: "schema_violation",
        context: { problems: ["version: Field required"] },
      }),
    );
    render(<EvaluationClient />);
    await userEvent.click(await screen.findByRole("button", { name: "Add benchmark" }));
    await userEvent.click(screen.getByRole("button", { name: "Start from an example" }));
    await userEvent.click(screen.getByRole("button", { name: "Register" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("the benchmark file is not valid");
    expect(alert).toHaveTextContent("version: Field required");
  });

  it("asks again while a run is still on the queue", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      vi.mocked(api.listEvaluationRuns).mockResolvedValue([run({ status: "queued", metrics: {} })]);
      render(<EvaluationClient />);
      await screen.findByText("queued");
      const before = vi.mocked(api.listEvaluationRuns).mock.calls.length;
      await vi.advanceTimersByTimeAsync(4100);
      expect(vi.mocked(api.listEvaluationRuns).mock.calls.length).toBeGreaterThan(before);
    } finally {
      vi.useRealTimers();
    }
  });

  it("cancels a queued run", async () => {
    vi.mocked(api.listEvaluationRuns).mockResolvedValue([run({ status: "queued", metrics: {} })]);
    vi.mocked(api.cancelEvaluationRun).mockResolvedValue(run({ status: "cancelled" }));
    render(<EvaluationClient />);
    await userEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(api.cancelEvaluationRun).toHaveBeenCalledWith("run-1");
  });

  it("compares two finished runs", async () => {
    vi.mocked(api.listEvaluationRuns).mockResolvedValue([
      run(),
      run({ id: "run-2", release_id: "rel-1" }),
    ]);
    vi.mocked(api.compareEvaluationRuns).mockResolvedValue({
      base: { id: "run-1", benchmark: "rla-retrieval@1.0.0", release_id: "rel-2" },
      candidate: { id: "run-2", benchmark: "rla-retrieval@1.0.0", release_id: "rel-1" },
      same_version: true,
      metrics: [{ name: "recall@5", base: 0.5, candidate: 0.75, delta: 0.25 }],
      gates: [{ name: "retrieval:recall@5", base: false, candidate: true }],
      changed_cases: [{ case_id: "q1", only_in: null, changes: { hits: { base: 0, candidate: 1 } } }],
      unchanged_cases: 3,
    });
    render(<EvaluationClient />);
    const compare = await screen.findByRole("button", { name: "Compare selected (0/2)" });
    expect(compare).toBeDisabled();
    await userEvent.click(screen.getByRole("checkbox", { name: "Compare rla-retrieval@1.0.0 on corpus@1.1.0" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Compare rla-retrieval@1.0.0 on corpus@1.0.0" }));
    await userEvent.click(screen.getByRole("button", { name: "Compare selected (2/2)" }));

    expect(api.compareEvaluationRuns).toHaveBeenCalledWith("run-1", "run-2");
    expect(await screen.findByText("+0.250")).toBeInTheDocument();
    expect(screen.getByText("1 case changed, 3 unchanged")).toBeInTheDocument();
    expect(screen.getByText("hits: 0 → 1")).toBeInTheDocument();
  });

  it("requires a benchmark for publishing", async () => {
    render(<EvaluationClient />);
    await userEvent.click(await screen.findByRole("button", { name: "Require for publishing" }));
    expect(api.setReleaseGate).toHaveBeenCalledWith("rla-retrieval", "1.0.0");
  });

  it("shows a required benchmark and lets it be lifted", async () => {
    vi.mocked(api.listReleaseGates).mockResolvedValue({
      items: [{ benchmark_id: "rla-retrieval", version: "1.0.0", key: "rla-retrieval@1.0.0", set_by: null, set_at: null }],
    });
    render(<EvaluationClient />);
    expect(await screen.findByText("required_for_publishing")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Stop requiring" }));
    expect(api.setReleaseGate).toHaveBeenCalledWith("rla-retrieval", null);
  });

  it("assigns a generation run's answers for human review", async () => {
    const generation = run({ id: "run-g", kind: "generation", passed: true, gates: { g: true }, failures: [] });
    vi.mocked(api.listEvaluationRuns).mockResolvedValue([generation]);
    vi.mocked(api.getEvaluationRun).mockResolvedValue(generation);
    render(<EvaluationClient />);
    await userEvent.click(await screen.findByRole("button", { name: "Details" }));
    const section = await screen.findByRole("region", { name: "Human review" });
    expect(section).toHaveTextContent("1 of 2 answers graded.");
    expect(section).toHaveTextContent("corpus gap ×1");

    await userEvent.click(screen.getByRole("checkbox", { name: "Rev Iewer" }));
    await userEvent.clear(screen.getByRole("spinbutton", { name: "Answers to sample" }));
    await userEvent.type(screen.getByRole("spinbutton", { name: "Answers to sample" }), "5");
    await userEvent.click(screen.getByRole("button", { name: "Assign answers" }));
    expect(api.assignEvaluationReviews).toHaveBeenCalledWith("run-g", {
      reviewer_ids: ["u7"],
      sample_size: 5,
    });
    expect(await screen.findByText("1 answer assigned.")).toBeInTheDocument();
  });

  it("opens a registered benchmark in the editor and registers the next version", async () => {
    vi.mocked(api.getBenchmark).mockResolvedValue({
      ...BENCHMARK,
      definition: {
        benchmark_id: "rla-retrieval",
        version: "1.0.0",
        kind: "retrieval",
        description: "Lab questions",
        origin: "lab",
        licence: "internal",
        permitted_use: "testing",
        cases: [{ case_id: "q1", question: "Which strain?", relevant: ["doi:10.1/a"] }],
        thresholds: { "recall@5": 0.6 },
      },
    });
    vi.mocked(api.registerBenchmark).mockResolvedValue({ ...BENCHMARK, version: "1.0.1", key: "rla-retrieval@1.0.1", created: true });
    render(<EvaluationClient />);
    await userEvent.click(await screen.findByRole("button", { name: "Edit as new version" }));
    expect(api.getBenchmark).toHaveBeenCalledWith("rla-retrieval", "1.0.0");
    await userEvent.click(await screen.findByRole("button", { name: "Register rla-retrieval@1.0.1" }));
    expect(api.registerBenchmark).toHaveBeenCalledWith(expect.objectContaining({ version: "1.0.1" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Registered rla-retrieval@1.0.1.");
  });
});
