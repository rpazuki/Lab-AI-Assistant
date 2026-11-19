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
});
