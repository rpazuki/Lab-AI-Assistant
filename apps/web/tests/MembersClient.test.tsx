import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MembersClient } from "@/components/MembersClient";
import { ApiError } from "@/lib/api";
import type { Invitation, Me, Member } from "@/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getMe: vi.fn(),
    listMembers: vi.fn(),
    listInvitations: vi.fn(),
    createInvitation: vi.fn(),
    revokeInvitation: vi.fn(async () => ({ id: "i1", state: "revoked" })),
    updateMember: vi.fn(),
    revokeMemberSessions: vi.fn(async () => ({ user_id: "u2" })),
  };
});

const api = await import("@/lib/api");

const ME: Me = {
  user_id: "u1",
  email: "admin@lab.invalid",
  full_name: "Lab Admin",
  workspace: { id: "w1", slug: "rlalab", name: "RLA Lab" },
  role: "admin",
  capabilities: ["manage_members"],
};

function member(overrides: Partial<Member>): Member {
  return {
    user_id: "u2",
    email: "researcher@lab.invalid",
    full_name: null,
    role: "researcher",
    status: "active",
    extra_capabilities: [],
    capabilities: ["ask"],
    accepted_at: "2026-11-01T10:00:00Z",
    last_login_at: null,
    account_active: true,
    ...overrides,
  };
}

const OPEN: Invitation = {
  id: "i1",
  email: "new@lab.invalid",
  role: "reviewer",
  state: "open",
  created_at: "2026-11-18T10:00:00Z",
  expires_at: "2026-11-25T10:00:00Z",
  accepted_at: null,
  invited_by: "u1",
};

describe("MembersClient", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getMe).mockResolvedValue(ME);
    vi.mocked(api.listMembers).mockResolvedValue([
      member({ user_id: "u1", email: "admin@lab.invalid", full_name: "Lab Admin", role: "admin" }),
      member({}),
    ]);
    vi.mocked(api.listInvitations).mockResolvedValue([OPEN]);
  });

  it("lists members and keeps an admin's own row out of reach", async () => {
    render(<MembersClient />);
    expect(await screen.findByText("(you)")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Role of admin@lab.invalid" })).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "Role of researcher@lab.invalid" })).toBeEnabled();
  });

  it("creates an invitation and shows its link once", async () => {
    vi.mocked(api.createInvitation).mockResolvedValue({
      id: "i2",
      email: "late@lab.invalid",
      role: "researcher",
      expires_at: "2026-11-26T10:00:00Z",
      token: "a-token-long-enough-to-pass",
    });
    render(<MembersClient />);
    await screen.findByText("(you)");
    await userEvent.type(screen.getByRole("textbox", { name: "Email" }), "late@lab.invalid");
    await userEvent.click(screen.getByRole("button", { name: "Create link" }));

    expect(api.createInvitation).toHaveBeenCalledWith({
      email: "late@lab.invalid",
      role: "researcher",
      ttl_hours: 168,
    });
    const link = await screen.findByRole("textbox", { name: "Invitation link" });
    // The token is a fragment, so it never reaches a server log.
    expect((link as HTMLInputElement).value).toMatch(/\/invite#token=a-token-long-enough-to-pass$/);
  });

  it("changes a role and says so", async () => {
    vi.mocked(api.updateMember).mockResolvedValue(member({ role: "reviewer" }));
    render(<MembersClient />);
    const select = await screen.findByRole("combobox", { name: "Role of researcher@lab.invalid" });
    await userEvent.selectOptions(select, "reviewer");
    expect(api.updateMember).toHaveBeenCalledWith("u2", { role: "reviewer" });
    expect(await screen.findByRole("status")).toHaveTextContent("researcher@lab.invalid is now reviewer.");
  });

  it("suspends only after confirmation", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    vi.mocked(api.updateMember).mockResolvedValue(member({ status: "suspended" }));
    render(<MembersClient />);
    await screen.findByText("(you)");
    const rows = screen.getAllByRole("row");
    const researcher = rows.find((item) => item.textContent?.includes("researcher@lab.invalid"));
    const suspend = within(researcher as HTMLElement).getByRole("button", { name: "Suspend" });

    await userEvent.click(suspend);
    expect(api.updateMember).not.toHaveBeenCalled();
    await userEvent.click(suspend);
    expect(api.updateMember).toHaveBeenCalledWith("u2", { status: "suspended" });
    confirm.mockRestore();
  });

  it("offers reinstatement to a suspended member", async () => {
    vi.mocked(api.listMembers).mockResolvedValue([member({ status: "suspended" })]);
    render(<MembersClient />);
    expect(await screen.findByRole("button", { name: "Reinstate" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Sign out everywhere" })).toBeDisabled();
  });

  it("revokes an open invitation", async () => {
    render(<MembersClient />);
    await userEvent.click(await screen.findByRole("button", { name: "Revoke" }));
    expect(api.revokeInvitation).toHaveBeenCalledWith("i1");
  });

  it("shows the API's refusal", async () => {
    vi.mocked(api.updateMember).mockRejectedValue(
      new ApiError({
        type: "about:blank",
        title: "Forbidden",
        status: 403,
        detail: "only an admin can grant or change the admin role",
        error_class: "policy_blocked",
      }),
    );
    render(<MembersClient />);
    const select = await screen.findByRole("combobox", { name: "Role of researcher@lab.invalid" });
    await userEvent.selectOptions(select, "admin");
    expect(await screen.findByRole("alert")).toHaveTextContent("only an admin can grant");
  });
});
