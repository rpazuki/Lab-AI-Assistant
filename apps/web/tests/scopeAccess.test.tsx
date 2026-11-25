import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ScopeAccessRules } from "@/components/ScopeReview";
import type { ScopeAccess, ScopeVersion } from "@/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    scopeAccess: vi.fn(),
    grantScopeAccess: vi.fn(),
    revokeScopeAccess: vi.fn(),
  };
});

const api = await import("@/lib/api");

const scope: ScopeVersion = {
  id: "sv-1",
  scope_id: "s-1",
  version: 2,
  name: "Marine toxicology",
  approval_state: "approved",
  connectors: ["pubmed"],
  scientific_objective: "o",
  concepts: [],
  exclusion_terms: [],
  relevance_policy: { include_threshold: 0.7, exclude_threshold: 0.3 },
  intended_uses: [],
  connector_plan: [],
  created_at: null,
  frozen_at: null,
};

const view = (rules: ScopeAccess["rules"]): ScopeAccess => ({
  scope_id: "s-1",
  name: "Marine toxicology",
  rules,
  roles: ["researcher", "reviewer", "data_steward", "service_account"],
  members: [{ user_id: "u1", role: "admin", reaches_everything: true }],
});

/**
 * Access is held on the scope, so what this panel shows is the whole canopy's
 * rules. With none, only admins reach it — and saying so plainly is the point,
 * because an empty list otherwise reads as "everyone".
 */
describe("Who may reach a scope", () => {
  beforeEach(() => vi.clearAllMocks());

  it("says that no rule means admins only, and does not offer the admin role", async () => {
    vi.mocked(api.scopeAccess).mockResolvedValue(view([]));

    render(<ScopeAccessRules scope={scope} />);

    expect(await screen.findByText(/only admins reach this scope/)).toBeInTheDocument();
    const menu = screen.getByRole("combobox", { name: "Role to let reach this scope" });
    expect(within(menu).queryByRole("option", { name: "admin" })).toBeNull();
    expect(within(menu).getByRole("option", { name: "researcher" })).toBeInTheDocument();
  });

  it("adds a rule for a role and reads the list again", async () => {
    vi.mocked(api.scopeAccess).mockResolvedValue(view([]));
    vi.mocked(api.grantScopeAccess).mockResolvedValue({ id: "g1" });
    const user = userEvent.setup();
    render(<ScopeAccessRules scope={scope} />);

    await user.selectOptions(
      await screen.findByRole("combobox", { name: "Role to let reach this scope" }),
      "reviewer",
    );
    await user.click(screen.getByRole("button", { name: "Add rule" }));

    await waitFor(() =>
      expect(api.grantScopeAccess).toHaveBeenCalledWith("sv-1", { role: "reviewer" }),
    );
    expect(api.scopeAccess).toHaveBeenCalledTimes(2);
  });

  it("marks the rules carried over when access control was switched on", async () => {
    vi.mocked(api.scopeAccess).mockResolvedValue(
      view([
        {
          id: "g1",
          subject_type: "role",
          subject_role: "researcher",
          subject_id: null,
          effect: "allow",
          policy_basis: "migrated_existing_scope",
        },
      ]),
    );

    render(<ScopeAccessRules scope={scope} />);

    expect(await screen.findByText("Everyone with the researcher role")).toBeInTheDocument();
    expect(screen.getByText(/carried over when access control was switched on/)).toBeInTheDocument();
  });

  it("removes a rule", async () => {
    vi.mocked(api.scopeAccess).mockResolvedValue(
      view([
        {
          id: "g1",
          subject_type: "role",
          subject_role: "reviewer",
          subject_id: null,
          effect: "allow",
          policy_basis: "steward_decision",
        },
      ]),
    );
    vi.mocked(api.revokeScopeAccess).mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(<ScopeAccessRules scope={scope} />);

    await user.click(
      await screen.findByRole("button", {
        name: "Remove the rule for Everyone with the reviewer role",
      }),
    );

    await waitFor(() => expect(api.revokeScopeAccess).toHaveBeenCalledWith("sv-1", "g1"));
  });

  it("keeps the refusal on screen when the server will not write the rule", async () => {
    vi.mocked(api.scopeAccess).mockResolvedValue(view([]));
    vi.mocked(api.grantScopeAccess).mockRejectedValue(new Error("missing capability manage_policy"));
    const user = userEvent.setup();
    render(<ScopeAccessRules scope={scope} />);

    await user.click(await screen.findByRole("button", { name: "Add rule" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("missing capability manage_policy");
  });
});
