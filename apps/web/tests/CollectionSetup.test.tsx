import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CollectionSetup } from "@/components/CollectionSetup";
import type { ScopeVersion } from "@/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listScopes: vi.fn(),
    listDiscoveryRuns: vi.fn(async () => ({ items: [], next_cursor: null, total: 0 })),
    createScope: vi.fn(async () => ({ id: "v2", version: 2, checksum: "c" })),
    updateScopeDraft: vi.fn(async () => ({ id: "v1", version: 1, checksum: "c" })),
    deleteScopeDraft: vi.fn(async () => undefined),
    checkScopeGate: vi.fn(async () => ({
      passed: false,
      blockers: ["scope does not state its intended uses"],
      compiled_connectors: ["pubmed"],
      gold_seed_recall: null,
      estimated_requests: 10,
    })),
    approveScope: vi.fn(),
    startDiscovery: vi.fn(),
    freezeDiscovery: vi.fn(),
    previewScopeRules: vi.fn(),
    applyScopeRules: vi.fn(),
    listReviewHistory: vi.fn(),
    previewScopeDeletion: vi.fn(),
    deleteScope: vi.fn(),
  };
});

const api = await import("@/lib/api");

/** A one-page answer from a paged endpoint. */
const page = <T,>(items: T[]) => ({ items, next_cursor: null, total: items.length });

function scope(overrides: Partial<ScopeVersion> = {}): ScopeVersion {
  return {
    id: "v1",
    scope_id: "s1",
    version: 1,
    name: "Yarrowia lipolytica",
    approval_state: "draft",
    connectors: ["pubmed"],
    scientific_objective: "Yarrowia literature",
    concepts: ["lipids"],
    exclusion_terms: [],
    relevance_policy: { include_threshold: 0.75, exclude_threshold: 0.25 },
    intended_uses: [],
    connector_plan: [{ connector_id: "pubmed", query: '"Yarrowia lipolytica"', required: false }],
    created_at: null,
    frozen_at: null,
    ...overrides,
  };
}

describe("CollectionSetup", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  const expectedFields = {
    name: "Yarrowia lipolytica",
    scientific_objective: "Yarrowia literature",
    intended_uses: ["literature search"],
    concepts: ["lipids"],
    exclusion_terms: [],
    include_threshold: 0.75,
    exclude_threshold: 0.25,
    connectors: [{ connector_id: "pubmed", query: '"Yarrowia lipolytica"', required: false }],
  };

  async function openDraftAndAddUse() {
    vi.mocked(api.listScopes).mockResolvedValue([scope()]);
    const user = userEvent.setup();
    render(<CollectionSetup />);
    await user.click(await screen.findByRole("button", { name: "Check gate" }));
    expect(await screen.findByText(/Select Edit to fix it/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Edit" }));
    const form = screen.getByRole("form", { name: "Edit Yarrowia lipolytica" });
    expect(within(form).getByLabelText("Purpose")).toHaveValue("Yarrowia literature");
    expect(within(form).getByLabelText("PubMed query")).toHaveValue('"Yarrowia lipolytica"');
    await user.type(within(form).getByLabelText("Intended uses (one per line)"), "literature search");
    return { user, form };
  }

  it("corrects a draft that failed its gate in place", async () => {
    const { user, form } = await openDraftAndAddUse();
    await user.click(within(form).getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(api.updateScopeDraft).toHaveBeenCalledWith("v1", expectedFields));
    expect(api.createScope).not.toHaveBeenCalled();
    expect(await screen.findByText(/v1 corrected/)).toBeInTheDocument();
  });

  it("or saves the edit as the scope's next version", async () => {
    const { user, form } = await openDraftAndAddUse();
    await user.click(within(form).getByRole("button", { name: "Save as v2" }));

    await waitFor(() =>
      expect(api.createScope).toHaveBeenCalledWith({ ...expectedFields, scope_id: "s1" }),
    );
    expect(api.updateScopeDraft).not.toHaveBeenCalled();
    expect(await screen.findByText(/v2 saved as a draft/)).toBeInTheDocument();
  });

  it("deletes a draft only after confirmation", async () => {
    vi.mocked(api.listScopes).mockResolvedValue([scope()]);
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    const user = userEvent.setup();
    render(<CollectionSetup />);

    await user.click(await screen.findByRole("button", { name: "Delete" }));
    expect(api.deleteScopeDraft).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api.deleteScopeDraft).toHaveBeenCalledWith("v1"));
    expect(await screen.findByText(/v1 deleted/)).toBeInTheDocument();
    confirm.mockRestore();
  });

  it("follows a discovery run until its sources have answered", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      vi.mocked(api.listScopes).mockResolvedValue([scope({ approval_state: "approved" })]);
      const searching = {
        id: "run-1",
        status: "running",
        started_at: null,
        frozen_at: null,
        degraded_reason: null,
        metrics: {},
        connectors: [
          {
            connector_id: "pubmed",
            required: true,
            outcome: null,
            records_seen: 20,
            declared_total: 90,
            accepted_degraded: false,
          },
        ],
      };
      vi.mocked(api.listDiscoveryRuns)
        .mockResolvedValueOnce(page([searching]))
        .mockResolvedValue(page([
          {
            ...searching,
            status: "degraded",
            connectors: [{ ...searching.connectors[0]!, outcome: "succeeded", records_seen: 90 }],
          },
        ]));
      render(<CollectionSetup />);

      expect(await screen.findByText("20")).toBeInTheDocument();
      expect(screen.getByText(/Updating live/)).toBeInTheDocument();
      await act(() => vi.advanceTimersByTimeAsync(3100));
      // Seen has caught up with declared, and the source reports its outcome.
      expect(await screen.findByText("succeeded")).toBeInTheDocument();
      expect(screen.getAllByText("90")).toHaveLength(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("offers Edit and Delete on unapproved versions only", async () => {
    vi.mocked(api.listScopes).mockResolvedValue([
      scope(),
      scope({ id: "v2", version: 2 }),
      scope({ id: "a1", scope_id: "s2", name: "Approved", approval_state: "approved" }),
      scope({ id: "r1", scope_id: "s3", name: "Retired", approval_state: "retired" }),
    ]);
    render(<CollectionSetup />);

    await screen.findByText("Approved");
    expect(screen.getAllByRole("button", { name: "Edit" })).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: "Delete" })).toHaveLength(2);
  });

  const frozen = (overrides: Partial<ScopeVersion> = {}) =>
    scope({
      approval_state: "approved",
      concepts: ["yarrowia | y. lipolytica"],
      exclusion_terms: ["plant"],
      frozen_at: "2026-11-22T12:03:39Z",
      ...overrides,
    });

  it("shows a frozen version read-only, with no way to save it", async () => {
    vi.mocked(api.listScopes).mockResolvedValue([frozen()]);
    const user = userEvent.setup();
    render(<CollectionSetup />);
    await user.click(await screen.findByRole("button", { name: "View Yarrowia lipolytica v1" }));

    const form = screen.getByRole("form", { name: "Yarrowia lipolytica v1, read-only" });
    expect(within(form).getByText(/approved and frozen/)).toBeInTheDocument();
    const concepts = within(form).getByLabelText(/^Concepts/);
    expect(concepts).toHaveValue("yarrowia | y. lipolytica");
    expect(concepts).toBeDisabled();
    expect(within(form).getByLabelText("Exclusion terms (one per line)")).toHaveValue("plant");
    expect(within(form).getByLabelText("PubMed query")).toBeDisabled();
    expect(within(form).queryByRole("button")).toBeNull();
  });

  it("shows a frozen version as saved: its own text, never the form's examples", async () => {
    vi.mocked(api.listScopes).mockResolvedValue([frozen({ exclusion_terms: [], concepts: [] })]);
    const user = userEvent.setup();
    render(<CollectionSetup />);
    await user.click(await screen.findByRole("button", { name: "View Yarrowia lipolytica v1" }));

    const form = screen.getByRole("form", { name: "Yarrowia lipolytica v1, read-only" });
    expect(within(form).getByLabelText("Purpose")).toHaveValue("Yarrowia literature");
    for (const field of within(form).getAllByRole("textbox")) {
      expect(field).not.toHaveAttribute("placeholder", expect.stringMatching(/./));
    }
  });

  it("deletes a whole scope only once nothing stops it and its name is typed", async () => {
    vi.mocked(api.listScopes).mockResolvedValue([frozen()]);
    vi.mocked(api.previewScopeDeletion).mockResolvedValue({
      scope_id: "s1",
      name: "Yarrowia lipolytica",
      versions: [{ id: "v1", version: 1, approval_state: "approved" }],
      counts: {
        versions: 1,
        runs: 2,
        papers: 28044,
        papers_kept: 0,
        decided_by_person: 6,
        files: 0,
        parsed_texts: 0,
        values: 0,
        value_reviews: 0,
        draft_releases: 0,
        tasks: 3,
      },
      blockers: [],
      deletable: true,
    });
    vi.mocked(api.deleteScope).mockResolvedValue({
      deleted: { versions: 1, runs: 2, papers: 28044 } as never,
    });
    const user = userEvent.setup();
    render(<CollectionSetup />);
    await user.click(
      await screen.findByRole("button", { name: "Delete the scope Yarrowia lipolytica, every version" }),
    );

    expect(await screen.findByText("28,044 papers")).toBeInTheDocument();
    const submit = screen.getByRole("button", { name: "Delete the scope" });
    expect(submit).toBeDisabled();
    await user.type(screen.getByLabelText(/to confirm/), "Yarrowia lipolytica");
    await user.click(submit);

    await waitFor(() => expect(api.deleteScope).toHaveBeenCalledWith("v1", "Yarrowia lipolytica"));
    expect(await screen.findByText(/Scope “Yarrowia lipolytica” deleted/)).toBeInTheDocument();
  });

  it("says why a scope cannot be deleted, and offers no button", async () => {
    vi.mocked(api.listScopes).mockResolvedValue([frozen()]);
    vi.mocked(api.previewScopeDeletion).mockResolvedValue({
      scope_id: "s1",
      name: "Yarrowia lipolytica",
      versions: [{ id: "v1", version: 1, approval_state: "approved" }],
      counts: {
        versions: 1,
        runs: 1,
        papers: 10,
        papers_kept: 0,
        decided_by_person: 0,
        files: 0,
        parsed_texts: 0,
        values: 0,
        value_reviews: 0,
        draft_releases: 0,
        tasks: 0,
      },
      blockers: ["release “Corpus” 1.0.0 is published and holds its papers"],
      deletable: false,
    });
    const user = userEvent.setup();
    render(<CollectionSetup />);
    await user.click(
      await screen.findByRole("button", { name: "Delete the scope Yarrowia lipolytica, every version" }),
    );

    expect(await screen.findByText(/is published and holds its papers/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete the scope" })).toBeNull();
  });

  it("starts the next version from a frozen one, which stays as it is", async () => {
    vi.mocked(api.listScopes).mockResolvedValue([frozen()]);
    const user = userEvent.setup();
    render(<CollectionSetup />);
    await user.click(
      await screen.findByRole("button", { name: "New version of Yarrowia lipolytica from v1" }),
    );
    const form = screen.getByRole("form", { name: "New version of Yarrowia lipolytica" });
    expect(within(form).queryByRole("button", { name: "Save changes" })).toBeNull();
    await user.type(within(form).getByLabelText("Exclusion terms (one per line)"), "\nmammalian");
    await user.type(within(form).getByLabelText("Intended uses (one per line)"), "literature search");
    await user.click(within(form).getByRole("button", { name: "Save as v2" }));

    await waitFor(() =>
      expect(api.createScope).toHaveBeenCalledWith(
        expect.objectContaining({
          scope_id: "s1",
          concepts: ["yarrowia | y. lipolytica"],
          exclusion_terms: ["plant", "mammalian"],
        }),
      ),
    );
    expect(api.updateScopeDraft).not.toHaveBeenCalled();
  });

  it("previews a frozen version's rules, then applies them as one step", async () => {
    vi.mocked(api.listScopes).mockResolvedValue([frozen()]);
    vi.mocked(api.previewScopeRules).mockResolvedValue({
      scope: {
        scope_version_id: "v1",
        name: "Yarrowia lipolytica",
        version: 1,
        concepts: ["yarrowia | y. lipolytica"],
        exclusion_terms: ["plant"],
        include_threshold: 0.75,
        exclude_threshold: 0.25,
      },
      considered: 28362,
      decided_by_person: 12,
      not_indexed: 0,
      unchanged: 3000,
      changes: 1500,
      outcome: { included: 1400, excluded: 100 },
      transitions: { "unreviewed>included": 1400, "unreviewed>excluded": 100 },
      reasons: [
        {
          relevance: "excluded",
          reason: "exclusion_term:plant",
          description: "mentions the exclusion term “plant”",
          count: 100,
        },
      ],
      samples: {},
      no_concepts: false,
    });
    vi.mocked(api.applyScopeRules)
      .mockResolvedValueOnce({ applied: 1000, remaining: 500, step_id: "step-1" })
      .mockResolvedValueOnce({ applied: 500, remaining: 0, step_id: "step-1" });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<CollectionSetup />);
    await user.click(
      await screen.findByRole("button", { name: "Apply the rules of Yarrowia lipolytica v1" }),
    );
    expect(await screen.findByText(/mentions the exclusion term “plant”/)).toBeInTheDocument();
    expect(screen.getByText(/12 were decided by a person/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Change 1,500 candidates" }));
    expect(await screen.findByText("1,500 candidates changed by v1's rules.")).toBeInTheDocument();
    expect(vi.mocked(api.applyScopeRules).mock.calls).toEqual([
      ["v1", null],
      ["v1", "step-1"],
    ]);
  });

  it("lists what was done under a frozen version", async () => {
    vi.mocked(api.listScopes).mockResolvedValue([frozen()]);
    vi.mocked(api.listReviewHistory).mockResolvedValue({
      items: [
        {
          id: "h2",
          kind: "relevance_by_filter",
          scope_version_id: "v1",
          actor: "Steward",
          rule: { title_words: ["ethanol"], to: "excluded" },
          counts: { excluded: 1500 },
          papers: [],
          subject_id: null,
          note: null,
          started_at: "2026-11-23T10:00:00Z",
          finished_at: null,
        },
        {
          id: "h1",
          kind: "scope_approved",
          scope_version_id: "v1",
          actor: "Steward",
          rule: { name: "Yarrowia lipolytica", version: 1 },
          counts: {},
          papers: [],
          subject_id: "v1",
          note: null,
          started_at: "2026-11-22T12:03:39Z",
          finished_at: null,
        },
      ],
      next_cursor: null,
      total: 2,
    });
    const user = userEvent.setup();
    render(<CollectionSetup />);
    await user.click(await screen.findByRole("button", { name: "History of Yarrowia lipolytica v1" }));

    expect(
      await screen.findByText("Set 1,500 excluded by filter (title has all of ethanol)."),
    ).toBeInTheDocument();
    expect(screen.getByText(/Approved “Yarrowia lipolytica” v1/)).toBeInTheDocument();
    expect(api.listReviewHistory).toHaveBeenCalledWith(
      expect.objectContaining({ scope_version_id: "v1" }),
    );
  });
});
