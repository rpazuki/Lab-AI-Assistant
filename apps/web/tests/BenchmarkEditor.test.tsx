import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  BenchmarkEditor,
  EMPTY_BENCHMARK,
  nextVersion,
  type BenchmarkDefinition,
} from "@/components/BenchmarkEditor";

const REGISTERED: BenchmarkDefinition = {
  benchmark_id: "rla-retrieval",
  version: "1.0.0",
  kind: "retrieval",
  description: "Lab questions",
  origin: "written by the lab",
  licence: "internal",
  permitted_use: "internal evaluation",
  sensitivity: "internal",
  cases: [{ case_id: "titre", question: "Which strain reached the highest titre?", relevant: ["doi:10.1/a"] }],
  thresholds: { "recall@5": 0.6 },
};

describe("nextVersion", () => {
  it.each([
    ["1.0.0", "1.0.1"],
    ["2.3.9", "2.3.10"],
    ["1", "1.1"],
    ["v-final", "v-final.1"],
  ])("%s becomes %s", (version, next) => {
    expect(nextVersion(version)).toBe(next);
  });
});

describe("BenchmarkEditor", () => {
  it("registers an edited benchmark as the next version", async () => {
    const onSave = vi.fn(async () => undefined);
    render(<BenchmarkEditor initial={REGISTERED} editing onSave={onSave} onCancel={() => undefined} />);
    expect(screen.getByRole("textbox", { name: "Version" })).toHaveValue("1.0.1");
    expect(screen.getByRole("textbox", { name: "Benchmark id" })).toBeDisabled();

    await userEvent.type(screen.getByLabelText(/Relevant papers/), "\npmid:123");
    await userEvent.click(screen.getByRole("button", { name: "Add a question" }));
    const questions = screen.getAllByRole("textbox", { name: "Question" });
    await userEvent.type(questions[1]!, "What yield on glycerol?");
    await userEvent.click(screen.getByRole("button", { name: "Register rla-retrieval@1.0.1" }));

    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({
        benchmark_id: "rla-retrieval",
        version: "1.0.1",
        origin: "written by the lab",
        cases: [
          { case_id: "titre", question: "Which strain reached the highest titre?", relevant: ["doi:10.1/a", "pmid:123"] },
          { case_id: "q2", question: "What yield on glycerol?", relevant: [] },
        ],
        thresholds: { "recall@5": 0.6 },
      }),
    );
  });

  it("refuses to register over the version it started from", async () => {
    const onSave = vi.fn(async () => undefined);
    render(<BenchmarkEditor initial={REGISTERED} editing onSave={onSave} onCancel={() => undefined} />);
    await userEvent.clear(screen.getByRole("textbox", { name: "Version" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Version" }), "1.0.0");
    await userEvent.click(screen.getByRole("button", { name: /Register/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already registered");
    expect(onSave).not.toHaveBeenCalled();
  });

  it("needs a question in every case and unique case ids", async () => {
    const onSave = vi.fn(async () => undefined);
    render(<BenchmarkEditor initial={EMPTY_BENCHMARK} editing={false} onSave={onSave} onCancel={() => undefined} />);
    await userEvent.type(screen.getByRole("textbox", { name: "Benchmark id" }), "lab-gen");
    await userEvent.click(screen.getByRole("button", { name: /Register/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Every case needs a question.");

    await userEvent.type(screen.getByRole("textbox", { name: "Question" }), "What titre?");
    await userEvent.click(screen.getByRole("button", { name: "Add a question" }));
    const ids = screen.getAllByRole("textbox", { name: "Case id" });
    await userEvent.clear(ids[1]!);
    await userEvent.type(ids[1]!, "q1");
    await userEvent.type(screen.getAllByRole("textbox", { name: "Question" })[1]!, "Again?");
    await userEvent.click(screen.getByRole("button", { name: /Register/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Case ids must be unique.");
    expect(onSave).not.toHaveBeenCalled();
  });

  it("warns before showing a blinded benchmark's gold answers", () => {
    render(
      <BenchmarkEditor initial={{ ...REGISTERED, blinded: true }} editing onSave={vi.fn()} onCancel={() => undefined} />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("blinded");
  });
});
