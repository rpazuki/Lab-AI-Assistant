import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MembersClient } from "@/components/MembersClient";
import { ApiError } from "@/lib/api";
import type { Invitation, Me, Member, Role } from "@/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getMe: vi.fn(),
    listMembers: vi.fn(),
    listInvitations: vi.fn(),
    listRoles: vi.fn(),
    createRole: vi.fn(async () => ({ id: "r1", name: "patent_holders" })),
    updateRole: vi.fn(async () => ({ id: "r1", name: "patent_holders" })),
    deleteRole: vi.fn(async () => ({ rules_removed: 0 })),
    createInvitation: vi.fn(),
    regenerateTemporaryPassword: vi.fn(),
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
    base_role: "researcher",
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

/** The four built-in roles as `/roles` returns them, plus any defined here. */
function roleList(...defined: Partial<Role>[]) {
  const built = ["researcher", "reviewer", "data_steward", "admin"].map((name) => ({
    id: null,
    name,
    display_name: name.replace(/_/g, " "),
    description: "",
    base_role: name,
    custom: false,
    members: 0,
    capabilities: [],
  }));
  return {
    items: [
      ...built,
      ...defined.map((role) => ({
        id: "r1",
        name: "patent_holders",
        display_name: "Patent holders",
        description: "Named on the patent filings",
        base_role: "researcher",
        custom: true,
        members: 0,
        capabilities: ["ask"],
        ...role,
      })),
    ] as Role[],
  };
}

const OPEN: Invitation = {
  id: "i1",
  email: "new@lab.invalid",
  role: "reviewer",
  state: "open",
  kind: "link",
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
    vi.mocked(api.listRoles).mockResolvedValue(roleList());
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
      kind: "link",
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

  it("drafts the email for a link, with the link in it", async () => {
    vi.mocked(api.createInvitation).mockResolvedValue({
      id: "i2",
      email: "late@lab.invalid",
      role: "researcher",
      kind: "link",
      expires_at: "2026-11-26T10:00:00Z",
      token: "a-token-long-enough-to-pass",
    });
    render(<MembersClient />);
    await screen.findByText("(you)");
    await userEvent.type(screen.getByRole("textbox", { name: "Email" }), "late@lab.invalid");
    await userEvent.click(screen.getByRole("button", { name: "Create link" }));

    const message = (await screen.findByRole("textbox", { name: "Message" })) as HTMLTextAreaElement;
    expect(message.value).toContain("join RLA Lab on the Lab AI Assistant as a researcher");
    expect(message.value).toMatch(/invite#token=a-token-long-enough-to-pass/);
    expect(message.value).toContain("Lab Admin");
    expect(screen.getByRole("textbox", { name: "Subject" })).toHaveValue(
      "Your invitation to RLA Lab on the Lab AI Assistant",
    );
    // The draft is the admin's to change before it goes.
    await userEvent.type(message, " P.S. See you Monday.");
    const mail = screen.getByRole("link", { name: "Open in email app" });
    expect(mail.getAttribute("href")).toMatch(/^mailto:late%40lab\.invalid\?subject=/);
    expect(decodeURIComponent(mail.getAttribute("href") ?? "")).toContain("See you Monday.");
  });

  it("gives a temporary password instead of a link when asked", async () => {
    vi.mocked(api.createInvitation).mockResolvedValue({
      id: "i3",
      email: "late@lab.invalid",
      role: "reviewer",
      kind: "temporary_password",
      expires_at: "2026-11-26T10:00:00Z",
      token: "h7kq-3mzt-x9pe-4wbn",
    });
    render(<MembersClient />);
    await screen.findByText("(you)");
    await userEvent.type(screen.getByRole("textbox", { name: "Email" }), "late@lab.invalid");
    await userEvent.click(
      screen.getByRole("checkbox", { name: /Give a temporary password instead of a link/ }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Create temporary password" }));

    expect(api.createInvitation).toHaveBeenCalledWith({
      email: "late@lab.invalid",
      role: "researcher",
      ttl_hours: 168,
      temporary_password: true,
    });
    expect(await screen.findByRole("textbox", { name: "Temporary password" })).toHaveValue(
      "h7kq-3mzt-x9pe-4wbn",
    );
    const message = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
    expect(message.value).toContain("h7kq-3mzt-x9pe-4wbn");
    expect(message.value).toContain("Sign in with your email address (late@lab.invalid)");
    expect(message.value).toMatch(/\/login\n/);
    expect(message.value).toContain("choose a password of your own");
    expect(screen.queryByRole("textbox", { name: "Invitation link" })).toBeNull();
  });

  it("makes a new temporary password for one that ran out", async () => {
    vi.mocked(api.listInvitations).mockResolvedValue([
      { ...OPEN, id: "i4", state: "expired", kind: "temporary_password" },
      OPEN,
    ]);
    vi.mocked(api.regenerateTemporaryPassword).mockResolvedValue({
      id: "i5",
      email: "new@lab.invalid",
      role: "reviewer",
      kind: "temporary_password",
      expires_at: "2026-12-04T10:00:00Z",
      token: "p2wx-8nfr-k4ty-6dmc",
    });
    render(<MembersClient />);
    expect(await screen.findByText("By temporary password")).toBeInTheDocument();
    expect(screen.getByText("By link")).toBeInTheDocument();
    // A link has no password to renew.
    expect(screen.getAllByRole("button", { name: /New temporary password for/ })).toHaveLength(1);
    await userEvent.click(
      screen.getByRole("button", { name: "New temporary password for new@lab.invalid" }),
    );

    expect(api.regenerateTemporaryPassword).toHaveBeenCalledWith("i4");
    expect(await screen.findByRole("textbox", { name: "Temporary password" })).toHaveValue(
      "p2wx-8nfr-k4ty-6dmc",
    );
    expect((screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement).value).toContain(
      "as a reviewer",
    );
  });

  it("puts the workspace first, then members, roles, invitations and the invite form", async () => {
    render(<MembersClient />);
    await screen.findByText("(you)");
    const headings = screen.getAllByRole("heading", { level: 2 }).map((item) => item.textContent);
    expect(headings).toEqual(["Members", "Roles", "Invitations", "Invite someone"]);
    for (const title of headings) {
      expect(screen.getByRole("button", { name: `Hide ${title}` })).toBeInTheDocument();
    }
    // The totals come before every box, and do not fold.
    const tiles = screen.getByLabelText("Workspace usage");
    const members = screen.getByRole("heading", { level: 2, name: "Members" });
    expect(tiles.compareDocumentPosition(members) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Hide Workspace/ })).toBeNull();
  });

  it("shows status and last sign-in in one column, each sortable", async () => {
    vi.mocked(api.listMembers).mockResolvedValue(
      roster([
        member({ user_id: "u1", email: "admin@lab.invalid", full_name: "Lab Admin", role: "admin" }),
        member({ last_login_at: "2026-11-20T09:30:00Z" }),
      ]),
    );
    render(<MembersClient />);
    await screen.findByText("(you)");
    expect(screen.getByRole("button", { name: /^Status/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Last sign-in/ })).toBeInTheDocument();
    expect(screen.getByText("Never signed in")).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: /^Usage/ })).toBeInTheDocument();
    const members = screen.getAllByRole("table")[0] as HTMLElement;
    expect(within(members).getAllByRole("columnheader")).toHaveLength(5);
  });
});

/**
 * Roles an admin defines, for audiences the built-in four do not name.
 *
 * Defining one grants nothing: it creates a name, which a rule on a scope can
 * then be written about. The panel says so, because a page that let someone
 * believe otherwise would leave confidential material open while looking shut.
 */
describe("Roles", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getMe).mockResolvedValue(ME);
    vi.mocked(api.listMembers).mockResolvedValue(roster([member({})]));
    vi.mocked(api.listInvitations).mockResolvedValue([]);
    vi.mocked(api.listRoles).mockResolvedValue(roleList());
  });

  it("defines one from the name as it was typed", async () => {
    render(<MembersClient />);

    await userEvent.type(
      await screen.findByRole("textbox", { name: "Role name" }),
      "Patent holders",
    );
    await userEvent.type(
      screen.getByRole("textbox", { name: "What it is for" }),
      "Named on the filings",
    );
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Based on" }), "reviewer");
    await userEvent.click(screen.getByRole("button", { name: "Add role" }));

    expect(api.createRole).toHaveBeenCalledWith({
      name: "Patent holders",
      display_name: "Patent holders",
      description: "Named on the filings",
      base_role: "reviewer",
    });
  });

  it("says a new role reaches nothing until a scope is opened to it", async () => {
    render(<MembersClient />);
    await userEvent.type(
      await screen.findByRole("textbox", { name: "Role name" }),
      "Patent holders",
    );
    await userEvent.click(screen.getByRole("button", { name: "Add role" }));

    expect(await screen.findByText(/reaches nothing until you add it to a scope/)).toBeInTheDocument();
  });

  it("offers a defined role in the member and invitation menus", async () => {
    vi.mocked(api.listRoles).mockResolvedValue(roleList({}));
    render(<MembersClient />);

    const menu = await screen.findByRole("combobox", { name: "Role of researcher@lab.invalid" });
    expect(within(menu).getByRole("option", { name: "Patent holders" })).toBeInTheDocument();
    await userEvent.selectOptions(menu, "patent_holders");
    expect(api.updateMember).toHaveBeenCalledWith("u2", { role: "patent_holders" });
  });

  it("will not offer to remove a role somebody is still in", async () => {
    vi.mocked(api.listRoles).mockResolvedValue(roleList({ members: 2 }));
    render(<MembersClient />);

    expect(await screen.findByText("2 people")).toBeInTheDocument();
    expect(screen.getByText("Move everyone out of it to remove it")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Delete the Patent/ })).toBeNull();
  });

  it("removes one nobody holds", async () => {
    vi.mocked(api.listRoles).mockResolvedValue(roleList({}));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<MembersClient />);

    await userEvent.click(
      await screen.findByRole("button", { name: "Delete the Patent holders role" }),
    );

    expect(api.deleteRole).toHaveBeenCalledWith("r1");
  });

  it("changes what the people in a role may do", async () => {
    vi.mocked(api.listRoles).mockResolvedValue(roleList({}));
    render(<MembersClient />);

    await userEvent.selectOptions(
      await screen.findByRole("combobox", { name: "Based on, for Patent holders" }),
      "data_steward",
    );

    expect(api.updateRole).toHaveBeenCalledWith("r1", { base_role: "data_steward" });
  });

  it("corrects how a role reads", async () => {
    vi.mocked(api.listRoles).mockResolvedValue(roleList({ members: 3 }));
    render(<MembersClient />);

    await userEvent.click(
      await screen.findByRole("button", { name: "Change the wording of patent_holders" }),
    );
    const shown = screen.getByRole("textbox", { name: "Shown as, for patent_holders" });
    await userEvent.clear(shown);
    await userEvent.type(shown, "Patent inventors");
    const purpose = screen.getByRole("textbox", { name: "What patent_holders is for" });
    await userEvent.clear(purpose);
    await userEvent.type(purpose, "Named on the EP filings");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(api.updateRole).toHaveBeenCalledWith("r1", {
      display_name: "Patent inventors",
      description: "Named on the EP filings",
    });
  });

  it("keeps the name rules are written about out of the correction", async () => {
    vi.mocked(api.listRoles).mockResolvedValue(roleList({}));
    render(<MembersClient />);

    await userEvent.click(
      await screen.findByRole("button", { name: "Change the wording of patent_holders" }),
    );

    // Nothing on the form edits the slug, and the form says why: every rule and
    // every membership holds it, so a correction here must not move it.
    expect(screen.getByText(/which stays as it is/)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Shown as, for patent_holders" })).toHaveValue(
      "Patent holders",
    );
  });

  it("writes nothing when a correction is cancelled", async () => {
    vi.mocked(api.listRoles).mockResolvedValue(roleList({}));
    render(<MembersClient />);

    await userEvent.click(
      await screen.findByRole("button", { name: "Change the wording of patent_holders" }),
    );
    await userEvent.type(
      screen.getByRole("textbox", { name: "Shown as, for patent_holders" }),
      " and friends",
    );
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(api.updateRole).not.toHaveBeenCalled();
    // The row is back as it was, so the typing went nowhere.
    expect(
      await screen.findByRole("button", { name: "Change the wording of patent_holders" }),
    ).toBeInTheDocument();
  });
});
