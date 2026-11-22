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
    listDiscoveryRuns: vi.fn(async () => []),
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
  };
});

const api = await import("@/lib/api");

function scope(overrides: Partial<ScopeVersion> = {}): ScopeVersion {
  return {
    id: "v1",
    scope_id: "s1",
    version: 1,
    name: "Yarrowia lipolytica",
    approval_state: "draft",
    connectors: ["pubmed"],
    scientific_objective: "Yarrowia literature",
    inclusion_criteria: ["Y. lipolytica is studied"],
    exclusion_criteria: [],
    concepts: ["lipids"],
    intended_uses: [],
    connector_plan: [{ connector_id: "pubmed", query: '"Yarrowia lipolytica"', required: false }],
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
    inclusion_criteria: ["Y. lipolytica is studied"],
    exclusion_criteria: [],
    intended_uses: ["literature search"],
    concepts: ["lipids"],
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
    expect(within(form).getByLabelText("Scientific objective")).toHaveValue("Yarrowia literature");
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
        .mockResolvedValueOnce([searching])
        .mockResolvedValue([
          {
            ...searching,
            status: "degraded",
            connectors: [{ ...searching.connectors[0]!, outcome: "succeeded", records_seen: 90 }],
          },
        ]);
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
});
