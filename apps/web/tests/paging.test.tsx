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
    listDiscoveryRuns: vi.fn(async () => ({ items: [], next_cursor: null, total: 0 })),
    listCandidates: vi.fn(),
    fetchIncluded: vi.fn(),
    listPlans: vi.fn(async () => ({ items: [], next_cursor: null, total: 0, status_counts: {} })),
    listConflicts: vi.fn(async () => ({
      conflict_total: 0,
      proposal_total: 0,
      next_cursor: null,
      duplicate_proposals: [],
    })),
    candidateFacets: vi.fn(async () => ({
      kinds: { article: 51 },
      sources: { pubmed: 51 },
      year_min: 2000,
      year_max: 2026,
      summarised: 51,
      works: 51,
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
    expect(await screen.findAllByText("1–50 of 51 candidates")).toHaveLength(2); // at the top and under the list
    expect(screen.queryByText("Paper 51")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Next page of candidates" }));
    expect(await screen.findByText("Paper 51")).toBeInTheDocument();
    expect(screen.getAllByText("51–51 of 51 candidates")).toHaveLength(2);
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
    await screen.findAllByText("1–50 of 51 candidates");
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

describe("Fetching included papers", () => {
  it("plans what has no plan, retries what can still be fetched, and says how many", async () => {
    vi.mocked(api.listCandidates).mockResolvedValue({ items: [], next_cursor: null, total: 0 });
    vi.mocked(api.fetchIncluded).mockResolvedValue({
      requeued: 1425,
      kept_for_a_person: 6467,
      already_tried: 12,
      planning_task_id: "t1",
    });
    const user = userEvent.setup();
    render(<CollectionsClient />);
    await user.click(await screen.findByRole("button", { name: "Fetch included papers" }));
    expect(api.fetchIncluded).toHaveBeenCalledTimes(1);
    expect(
      await screen.findByText(/1,425 fetches will be tried again\. 12 were already tried .* 6,467 can only come from an upload/),
    ).toBeInTheDocument();
  });
});

describe("Long lists on the Collections page", () => {
  it("say how many rows they hold at the top, set rows per page, and fold away", async () => {
    vi.mocked(api.listCandidates).mockImplementation(async (options = {}) => ({
      items: Array.from({ length: Math.min(options.limit ?? 50, 51) }, (_, i) => candidate(i + 1)),
      next_cursor: (options.limit ?? 50) < 51 ? "more" : null,
      total: 51,
    }));
    const user = userEvent.setup();
    render(<CollectionsClient />);
    expect(await screen.findAllByText("1–50 of 51 candidates")).toHaveLength(2);

    await user.selectOptions(screen.getByRole("combobox", { name: "Rows per page of candidates" }), "10");
    await waitFor(() => expect(api.listCandidates).toHaveBeenLastCalledWith(expect.objectContaining({ limit: 10 })));
    expect(await screen.findAllByText("1–10 of 51 candidates")).toHaveLength(2);

    const hide = screen.getByRole("button", { name: "Hide Candidates" });
    expect(hide).toHaveAttribute("aria-expanded", "true");
    await user.click(hide);
    expect(screen.getByRole("button", { name: "Show Candidates" })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("combobox", { name: "Rows per page of candidates" })).toBeNull();
    expect(screen.getByRole("button", { name: "Hide Acquisition queue" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Hide Duplicates and identity conflicts" })).toBeInTheDocument();
  });
});
