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
    listMemberSessions: vi.fn(async () => ({
      items: [
        {
          id: "s1",
          title: "Lipid yields",
          pinned_release_id: null,
          created_at: "2026-11-18T10:00:00Z",
          updated_at: "2026-11-18T10:05:00Z",
          deleted: true,
        },
      ],
      next_cursor: null,
      total: 1,
    })),
    getMemberSession: vi.fn(async () => ({
      id: "s1",
      title: "Lipid yields",
      pinned_release_id: null,
      created_at: null,
      updated_at: null,
      deleted: true,
      messages: [
        {
          id: "m1",
          role: "user",
          content: "What lipid yield was reported?",
          created_at: null,
          release_id: null,
          citations: [],
          citation_validation_passed: true,
          unsupported_handles: [],
        },
      ],
    })),
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
    token_limit: 1_000_000,
    custom_token_limit: false,
    usage: {
      sessions: 2,
      questions: 3,
      answers: 3,
      input_tokens: 1000,
      output_tokens: 500,
      total_tokens: 1500,
      last_active_at: null,
      avg_latency_ms: 800,
    },
    limit_reached: false,
    ...overrides,
  };
}

function roster(items: Member[]) {
  return { items, token_limit_default: 1_000_000, chat_admin_access: true };
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
    vi.mocked(api.listMembers).mockResolvedValue(
      roster([
        member({ user_id: "u1", email: "admin@lab.invalid", full_name: "Lab Admin", role: "admin" }),
        member({}),
      ]),
    );
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
    vi.mocked(api.listMembers).mockResolvedValue(roster([member({ status: "suspended" })]));
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

  it("shows workspace totals and each member's usage", async () => {
    render(<MembersClient />);
    const tiles = await screen.findByLabelText("Workspace usage");
    expect(tiles).toHaveTextContent("Questions asked6");
    expect(tiles).toHaveTextContent("Tokens used3k");
    expect(screen.getAllByText("1.5k / 1M tokens (default)")).toHaveLength(2);
  });

  it("sets a member's token limit", async () => {
    vi.mocked(api.updateMember).mockResolvedValue(member({ token_limit: 5000, custom_token_limit: true }));
    render(<MembersClient />);
    await screen.findByText("(you)");
    const rows = screen.getAllByRole("row");
    const researcher = rows.find((item) => item.textContent?.includes("researcher@lab.invalid")) as HTMLElement;
    await userEvent.click(within(researcher).getByRole("button", { name: "Change limit" }));
    const input = screen.getByRole("spinbutton", { name: "Token limit for researcher@lab.invalid" });
    await userEvent.clear(input);
    await userEvent.type(input, "5000");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(api.updateMember).toHaveBeenCalledWith("u2", { token_limit: 5000 });
  });

  it("returns a member with their own limit to the default", async () => {
    vi.mocked(api.listMembers).mockResolvedValue(
      roster([member({ token_limit: 5000, custom_token_limit: true })]),
    );
    vi.mocked(api.updateMember).mockResolvedValue(member({}));
    render(<MembersClient />);
    await userEvent.click(await screen.findByRole("button", { name: "Change limit" }));
    await userEvent.click(screen.getByRole("button", { name: "Use default (1M)" }));
    expect(api.updateMember).toHaveBeenCalledWith("u2", { token_limit: null });
  });

  it("reads a member's conversations behind an audit notice", async () => {
    render(<MembersClient />);
    await screen.findByText("(you)");
    const rows = screen.getAllByRole("row");
    const researcher = rows.find((item) => item.textContent?.includes("researcher@lab.invalid")) as HTMLElement;
    await userEvent.click(within(researcher).getByRole("button", { name: "Read chats" }));

    expect(await screen.findByText(/recorded in the audit log under your name/)).toBeInTheDocument();
    const session = await screen.findByRole("button", { name: /Lipid yields/ });
    expect(session).toHaveTextContent("deleted by the member");
    await userEvent.click(session);
    expect(await screen.findByText("What lipid yield was reported?")).toBeInTheDocument();
    expect(api.getMemberSession).toHaveBeenCalledWith("u2", "s1");
  });

  it("offers no chat reading when the installation turns it off", async () => {
    vi.mocked(api.listMembers).mockResolvedValue({ ...roster([member({})]), chat_admin_access: false });
    render(<MembersClient />);
    await screen.findByRole("button", { name: "Suspend" });
    expect(screen.queryByRole("button", { name: "Read chats" })).not.toBeInTheDocument();
  });
});
