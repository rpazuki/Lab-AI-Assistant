import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ProgressClient } from "@/components/ProgressClient";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    collectionProgress: vi.fn(async () => ({
      funnel: [
        { stage: "found", label: "Found", count: 28308 },
        { stage: "included", label: "Included", count: 28285 },
        { stage: "fetched", label: "Full text fetched", count: 3374 },
        { stage: "served", label: "In the release Ask serves", count: 0 },
      ],
      discovery: { runs: 1, by_source: { openalex: 20000, pubmed: 3131 }, merged: 73, open_duplicate_pairs: 4, identity_conflicts: 0 },
      screening: {
        relevance: { included: 28285, excluded: 23, unreviewed: 0, included_uncertain: 0 },
        decided_by_person: 6,
        with_abstract: 3446,
        included_with_abstract: 3440,
        found: 28308,
        included: 28285,
      },
      acquisition: {
        plans: { acquired: 2627, assisted: 24984, failed: 747 },
        waiting_reasons: { none: 14228, authentication_required: 2907 },
        failed_reasons: { identity_mismatch: 747 },
        identity: { identifier: 2626, unconfirmed: 747 },
        preprints: 0,
      },
      parsing: { by_status: { ready: 2285, degraded: 341 }, degraded_by_reason: { "reference list not separated": 200 } },
      extraction: { papers_with_values: 2000, values: 9000, waiting_review: 8500, tasks: { succeeded: 2285 } },
      daily: [{ day: "2026-11-25", found: 0, fetched: 120, parsed: 100 }],
    })),
  };
});

describe("Collection progress", () => {
  it("follows papers through each stage and says why they are held up", async () => {
    render(<ProgressClient />);
    const funnel = await screen.findByRole("list", { name: "Funnel" });
    expect(within(funnel).getByText("Full text fetched")).toBeInTheDocument();
    expect(within(funnel).getByText("3,374")).toBeInTheDocument();
    expect(within(funnel).getByText("11.9%")).toBeInTheDocument();

    const waiting = screen.getByRole("list", { name: "Why papers wait for a person" });
    expect(within(waiting).getByText("No open licence: only an upload is allowed")).toBeInTheDocument();
    expect(within(waiting).getByText("14,228")).toBeInTheDocument();
    expect(screen.getByText("The file could not be tied to the paper")).toBeInTheDocument();
    expect(screen.getByText("3,446 of 28,308")).toBeInTheDocument(); // papers with an abstract
  });
});
