import { render, screen, waitFor, within } from "@testing-library/react";
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

  it("reopens a draft that failed its gate and saves the fix as its next version", async () => {
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
    await user.click(within(form).getByRole("button", { name: "Save as v2" }));

    await waitFor(() => expect(api.createScope).toHaveBeenCalledTimes(1));
    expect(api.createScope).toHaveBeenCalledWith({
      name: "Yarrowia lipolytica",
      scientific_objective: "Yarrowia literature",
      inclusion_criteria: ["Y. lipolytica is studied"],
      exclusion_criteria: [],
      intended_uses: ["literature search"],
      concepts: ["lipids"],
      connectors: [{ connector_id: "pubmed", query: '"Yarrowia lipolytica"', required: false }],
      scope_id: "s1",
    });
    expect(await screen.findByText(/v2 saved as a draft/)).toBeInTheDocument();
  });

  it("offers Edit only on the newest draft version of a scope", async () => {
    vi.mocked(api.listScopes).mockResolvedValue([
      scope(),
      scope({ id: "v2", version: 2 }),
      scope({ id: "a1", scope_id: "s2", name: "Approved", approval_state: "approved" }),
    ]);
    render(<CollectionSetup />);

    await screen.findByText("Approved");
    expect(screen.getAllByRole("button", { name: "Edit" })).toHaveLength(1);
  });
});
