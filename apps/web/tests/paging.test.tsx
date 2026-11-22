import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CollectionsClient } from "@/components/CollectionsClient";
import type { Candidate } from "@/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listScopes: vi.fn(async () => []),
    listDiscoveryRuns: vi.fn(async () => []),
    listCandidates: vi.fn(),
    listPlans: vi.fn(async () => ({ items: [], next_cursor: null, total: 0, status_counts: {} })),
    listConflicts: vi.fn(async () => ({
      conflict_total: 0,
      proposal_total: 0,
      next_cursor: null,
      duplicate_proposals: [],
    })),
  };
});

const api = await import("@/lib/api");

function candidate(n: number): Candidate {
  return {
    work_id: `work-${n}`,
    title: `Paper ${n}`,
    year: 2024,
    journal: null,
    work_kind: "article",
    identity_status: "resolved",
    relevance: "unreviewed",
    retraction_status: "none",
    identifiers: [],
    provenance: {},
  };
}

describe("The candidate list is read a page at a time", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listCandidates).mockImplementation(async (options = {}) =>
      options.after
        ? { items: [candidate(51)], next_cursor: null, total: 51 }
        : {
            items: Array.from({ length: 50 }, (_, i) => candidate(i + 1)),
            next_cursor: "work-50",
            total: 51,
          },
    );
  });

  it("shows one page and the total, and moves between pages", async () => {
    const user = userEvent.setup();
    render(<CollectionsClient />);
    expect(await screen.findByText("1–50 of 51 candidates")).toBeInTheDocument();
    expect(screen.queryByText("Paper 51")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Next page of candidates" }));
    expect(await screen.findByText("Paper 51")).toBeInTheDocument();
    expect(screen.getByText("51–51 of 51 candidates")).toBeInTheDocument();
    expect(screen.queryByText("Paper 1")).toBeNull();
    expect(api.listCandidates).toHaveBeenLastCalledWith(
      expect.objectContaining({ after: "work-50", limit: 50 }),
    );

    await user.click(screen.getByRole("button", { name: "Previous page of candidates" }));
    expect(await screen.findByText("Paper 1")).toBeInTheDocument();
  });

  it("filters on the server and starts again from the first page", async () => {
    const user = userEvent.setup();
    render(<CollectionsClient />);
    await screen.findByText("1–50 of 51 candidates");
    await user.click(screen.getByRole("button", { name: "Next page of candidates" }));
    await screen.findByText("Paper 51");

    await user.selectOptions(screen.getByRole("combobox", { name: "Filter by relevance" }), "included");
    await waitFor(() =>
      expect(api.listCandidates).toHaveBeenLastCalledWith(
        expect.objectContaining({ relevance: "included", after: null }),
      ),
    );
  });
});
