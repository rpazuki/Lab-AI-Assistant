import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CandidateReview } from "@/components/CandidateReview";
import { DEFAULT_RULE, DuplicateReview } from "@/components/DuplicateReview";
import type { DuplicateProposal, PaperSummary } from "@/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listConflicts: vi.fn(),
    mergeDuplicate: vi.fn(async () => ({ decision_id: "d", kept_work_id: "a", merged_work_id: "b" })),
    dismissDuplicate: vi.fn(async () => ({ id: "p1", status: "dismissed" })),
    previewMergeRule: vi.fn(),
    applyMergeRule: vi.fn(),
    listIdentityConflicts: vi.fn(async () => ({ items: [], next_cursor: null, total: 0 })),
    keepConflictSeparate: vi.fn(async () => ({ work_id: "c1", identity_status: "resolved" })),
    keepConflictsSeparate: vi.fn(async () => ({ resolved: 2, remaining: 0 })),
    mergeWorks: vi.fn(async () => ({ decision_id: "d2", after: {} })),
    listCandidates: vi.fn(async () => ({ items: [], next_cursor: null, total: 240 })),
    candidateFacets: vi.fn(async () => ({
      kinds: { article: 200, dataset: 40 },
      sources: { pubmed: 240 },
      year_min: 2000,
      year_max: 2026,
      summarised: 240,
      works: 240,
    })),
    bulkRelevance: vi.fn(),
  };
});

const api = await import("@/lib/api");

function paper(id: string, title: string): PaperSummary {
  return {
    work_id: id,
    title,
    year: 2024,
    journal: "Metabolic Engineering",
    resource_kind: "journal-article",
    first_author: "Ledesma-Amaro",
    sources: ["pubmed"],
  };
}

const pair: DuplicateProposal = {
  id: "p1",
  left_work_id: "a",
  right_work_id: "b",
  similarity: 1,
  year_gap: 0,
  first_author_match: true,
  left: paper("a", "Lipid production in Yarrowia"),
  right: paper("b", "Lipid production in Yarrowia."),
};

describe("Duplicate review", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listConflicts).mockResolvedValue({
      conflict_total: 0,
      proposal_total: 1,
      next_cursor: null,
      duplicate_proposals: [pair],
    });
  });
  afterEach(() => vi.restoreAllMocks());

  it("shows both papers of a pair, and settles it with one click", async () => {
    const user = userEvent.setup();
    render(<DuplicateReview live={false} />);
    expect(await screen.findByText("Lipid production in Yarrowia")).toBeInTheDocument();
    expect(screen.getByText("Lipid production in Yarrowia.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Merge" }));
    expect(api.mergeDuplicate).toHaveBeenCalledWith("p1");
    await user.click(screen.getByRole("button", { name: "Not the same" }));
    expect(api.dismissDuplicate).toHaveBeenCalledWith("p1");
  });

  it("previews a rule, says why it skips pairs, then merges in rounds", async () => {
    vi.mocked(api.previewMergeRule).mockResolvedValue({
      considered: 7232,
      accepted_pairs: 1900,
      groups: 1850,
      works_merged: 1900,
      skipped: [
        {
          reason: "different_doi",
          description: "each has its own DOI: two publications, not one",
          count: 400,
          examples: [],
        },
      ],
      sample_groups: [],
    });
    vi.mocked(api.applyMergeRule)
      .mockResolvedValueOnce({ groups_merged: 200, works_merged: 205, remaining_groups: 1650 })
      .mockResolvedValueOnce({ groups_merged: 1650, works_merged: 1695, remaining_groups: 0 });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<DuplicateReview live={false} />);
    await screen.findByText("Lipid production in Yarrowia");

    await user.click(screen.getByRole("button", { name: "Preview" }));
    expect(api.previewMergeRule).toHaveBeenCalledWith(DEFAULT_RULE, {});
    expect(await screen.findByText(/each has its own DOI/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Merge 1,900 papers" }));
    expect(confirm.mock.calls[0]![0]).toMatch(/Merge 1,900 papers into 1,850/);
    expect(await screen.findByText("1,900 papers merged.")).toBeInTheDocument();
    expect(api.applyMergeRule).toHaveBeenCalledTimes(2);
  });

  it("forgets a preview once the rule changes", async () => {
    vi.mocked(api.previewMergeRule).mockResolvedValue({
      considered: 1,
      accepted_pairs: 1,
      groups: 1,
      works_merged: 1,
      skipped: [],
      sample_groups: [],
    });
    const user = userEvent.setup();
    render(<DuplicateReview live={false} />);
    await user.click(await screen.findByRole("button", { name: "Preview" }));
    expect(await screen.findByRole("button", { name: "Merge 1 paper" })).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "Same year" }));
    expect(screen.queryByRole("button", { name: "Merge 1 paper" })).toBeNull();
  });
});

describe("Candidate review", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(() => vi.restoreAllMocks());

  it("filters on the server and applies a decision to everything that matches", async () => {
    vi.mocked(api.bulkRelevance)
      .mockResolvedValueOnce({
        matched: 1500,
        sample: [paper("x", "A corn ethanol plant")],
        applied: 0,
        remaining: 1500,
      })
      .mockResolvedValueOnce({ matched: 1500, sample: [], applied: 1000, remaining: 500 })
      .mockResolvedValueOnce({ matched: 500, sample: [], applied: 500, remaining: 0 });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<CandidateReview live={false} />);

    await user.type(await screen.findByRole("textbox", { name: "Title has all of" }), "ethanol");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() =>
      expect(api.listCandidates).toHaveBeenLastCalledWith(
        expect.objectContaining({ title: "ethanol", after: null }),
      ),
    );

    await user.click(screen.getByRole("button", { name: "Exclude all" }));
    expect(confirm.mock.calls[0]![0]).toMatch(/Exclude 1,500 candidates not yet reviewed/);
    expect(confirm.mock.calls[0]![0]).toMatch(/A corn ethanol plant/);
    expect(await screen.findByText("1,500 candidates set to excluded.")).toBeInTheDocument();
    const calls = vi.mocked(api.bulkRelevance).mock.calls;
    expect(calls.map(([, body]) => body.apply)).toEqual([false, true, true]);
    expect(calls.every(([filters]) => filters.title === "ethanol")).toBe(true);
    expect(calls[0]![1]).toMatchObject({ relevance: "excluded", only_unreviewed: true });
  });

  it("changes nothing when the preview is declined", async () => {
    vi.mocked(api.bulkRelevance).mockResolvedValueOnce({
      matched: 3,
      sample: [],
      applied: 0,
      remaining: 3,
    });
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const user = userEvent.setup();
    render(<CandidateReview live={false} />);
    await user.click(await screen.findByRole("button", { name: "Include all" }));
    expect(api.bulkRelevance).toHaveBeenCalledTimes(1);
  });
});


describe("Searching and settling duplicates and conflicts", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    vi.mocked(api.listConflicts).mockResolvedValue({
      conflict_total: 2,
      proposal_total: 1,
      next_cursor: null,
      duplicate_proposals: [pair],
    });
  });

  it("searches pairs by title words, and the rule previews the same search", async () => {
    vi.mocked(api.previewMergeRule).mockResolvedValue({
      considered: 1,
      accepted_pairs: 1,
      groups: 1,
      works_merged: 1,
      skipped: [],
      sample_groups: [],
    });
    const user = userEvent.setup();
    render(<DuplicateReview live={false} />);
    await user.type(await screen.findByLabelText("Either title has all of"), "yarrowia");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() =>
      expect(api.listConflicts).toHaveBeenLastCalledWith(expect.objectContaining({ q: "yarrowia" })),
    );
    await user.click(screen.getByRole("button", { name: "Preview" }));
    expect(api.previewMergeRule).toHaveBeenCalledWith(DEFAULT_RULE, expect.objectContaining({ q: "yarrowia" }));
  });

  it("lists each conflict with the other paper, and keeps one or all separate", async () => {
    vi.mocked(api.listIdentityConflicts).mockResolvedValue({
      items: [
        {
          ...paper("c1", "Microbial fatty acid specificity"),
          identifiers: [{ scheme: "doi", value: "10.1007/bf02898718" }],
          with: [
            {
              ...paper("c2", "Microbial fatty acid specificity"),
              reason: "two_papers_matched_one_record",
              identifier: null,
            },
          ],
        },
      ],
      next_cursor: null,
      total: 2,
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<DuplicateReview live={false} />);
    await user.click(await screen.findByRole("tab", { name: "Identity conflicts (2)" }));

    expect(await screen.findByText("DOI 10.1007/bf02898718")).toBeInTheDocument();
    expect(screen.getByText(/could not be merged automatically/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Same paper: merge" }));
    expect(api.mergeWorks).toHaveBeenCalledWith("c1", "c2", "steward_same_paper");

    await user.click(screen.getByRole("button", { name: "Keep separate" }));
    expect(api.keepConflictSeparate).toHaveBeenCalledWith("c1");

    await user.type(screen.getByLabelText("Conflict title has all of"), "fatty");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() =>
      expect(api.listIdentityConflicts).toHaveBeenLastCalledWith(expect.objectContaining({ q: "fatty" })),
    );
    await user.click(screen.getByRole("button", { name: "Keep all 2 separate" }));
    expect(api.keepConflictsSeparate).toHaveBeenCalledWith("fatty");
    expect(await screen.findByText("2 papers kept separate.")).toBeInTheDocument();
  });
});
