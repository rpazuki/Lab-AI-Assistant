import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CLOSED_GROUPS_KEY, COLLAPSED_KEY, Sidebar } from "@/components/Sidebar";
import { ApiError } from "@/lib/api";
import type { Me } from "@/types";

let pathname = "/ask";
vi.mock("next/navigation", () => ({ usePathname: () => pathname }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, getMe: vi.fn(), logout: vi.fn(async () => undefined) };
});

const api = await import("@/lib/api");

function member(role: string, capabilities: string[]): Me {
  return {
    user_id: "u1",
    email: "member@lab.invalid",
    full_name: null,
    workspace: { id: "w1", slug: "rlalab", name: "RLA Lab" },
    role,
    capabilities,
  };
}

const ALL = [
  "ask", "search", "manage_scope", "run_discovery", "decide_identity", "decide_rights",
  "acquire", "upload_asset", "review_parse", "run_extraction", "review_claims",
  "manage_release", "approve_release", "export", "view_analytics", "manage_members",
  "manage_policy", "view_operations", "run_evaluation",
];

const sections = () =>
  within(screen.getByRole("navigation", { name: "Main" }))
    .queryAllByRole("link")
    .map((link) => link.textContent);

const groupHeaders = () =>
  within(screen.getByRole("navigation", { name: "Main" }))
    .queryAllByRole("button")
    .map((button) => button.textContent);

describe("Sidebar", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    pathname = "/ask";
  });
  afterEach(() => vi.unstubAllGlobals());

  it("offers only sign-in to a visitor without a session", async () => {
    pathname = "/login";
    vi.mocked(api.getMe).mockRejectedValue(
      new ApiError({
        type: "about:blank",
        title: "Unauthorized",
        status: 401,
        detail: "sign in",
        error_class: "authentication_required",
      }),
    );
    render(<Sidebar />);
    expect(await screen.findByRole("link", { name: "Sign in" })).toHaveAttribute("aria-current", "page");
    expect(sections()).toEqual(["Sign in"]);
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
  });

  it("shows no section while it is still asking who is signed in", () => {
    vi.mocked(api.getMe).mockReturnValue(new Promise(() => undefined));
    render(<Sidebar />);
    expect(sections()).toEqual([]);
  });

  it("gives a researcher only the sections a researcher can use", async () => {
    vi.mocked(api.getMe).mockResolvedValue(
      member("researcher", ["ask", "search", "upload_asset", "view_analytics"]),
    );
    render(<Sidebar />);
    expect(await screen.findByRole("button", { name: "Sign out" })).toBeInTheDocument();
    expect(sections()).toEqual(["Ask", "Analytics"]);
    expect(groupHeaders()).toEqual(["Research"]);
  });

  it("gives a reviewer the review queue but not releases", async () => {
    vi.mocked(api.getMe).mockResolvedValue(
      member("reviewer", ["ask", "search", "upload_asset", "view_analytics", "decide_identity", "review_parse", "review_claims"]),
    );
    render(<Sidebar />);
    await screen.findByRole("button", { name: "Sign out" });
    expect(sections()).toEqual(["Ask", "Analytics", "Collections", "Review"]);
  });

  it("gives an admin every section, grouped", async () => {
    vi.mocked(api.getMe).mockResolvedValue(member("admin", ALL));
    render(<Sidebar />);
    await screen.findByRole("button", { name: "Sign out" });
    expect(sections()).toEqual([
      "Ask", "Analytics", "Collections", "Review", "Releases", "Evaluation", "Members", "Operations",
    ]);
    expect(groupHeaders()).toEqual(["Research", "Curation", "Publishing", "Administration"]);
    expect(screen.getByRole("link", { name: "Ask" })).toHaveAttribute("aria-current", "page");
  });

  it("closes and reopens a group, and remembers it", async () => {
    vi.mocked(api.getMe).mockResolvedValue(member("admin", ALL));
    render(<Sidebar />);
    const header = await screen.findByRole("button", { name: "Publishing" });
    expect(header).toHaveAttribute("aria-expanded", "true");

    await userEvent.click(header);
    expect(header).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("link", { name: "Releases" })).not.toBeInTheDocument();
    expect(JSON.parse(window.localStorage.getItem(CLOSED_GROUPS_KEY) ?? "[]")).toEqual(["publishing"]);

    await userEvent.click(header);
    expect(screen.getByRole("link", { name: "Releases" })).toBeInTheDocument();
  });

  it("hides down to a rail of icons that keep their names", async () => {
    vi.mocked(api.getMe).mockResolvedValue(member("admin", ALL));
    render(<Sidebar />);
    await screen.findByRole("button", { name: "Sign out" });

    await userEvent.click(screen.getByRole("button", { name: "Hide navigation" }));
    const show = screen.getByRole("button", { name: "Show navigation" });
    expect(show).toHaveAttribute("aria-expanded", "false");
    expect(window.localStorage.getItem(COLLAPSED_KEY)).toBe("1");
    // No group headers in the rail, but every section is still a named link.
    expect(groupHeaders()).toEqual([]);
    expect(screen.getByRole("link", { name: "Members" })).toHaveAttribute("title", "Members");
    expect(screen.queryByText("Lab AI Assistant")).not.toBeInTheDocument();

    await userEvent.click(show);
    expect(screen.getByRole("button", { name: "Hide navigation" })).toBeInTheDocument();
  });

  it("opens hidden when it was hidden last time", async () => {
    window.localStorage.setItem(COLLAPSED_KEY, "1");
    vi.mocked(api.getMe).mockResolvedValue(member("admin", ALL));
    render(<Sidebar />);
    expect(await screen.findByRole("button", { name: "Show navigation" })).toBeInTheDocument();
  });

  it("signs out and reloads on the sign-in page", async () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    vi.mocked(api.getMe).mockResolvedValue(member("admin", ALL));
    render(<Sidebar />);
    (await screen.findByRole("button", { name: "Sign out" })).click();
    await waitFor(() => expect(assign).toHaveBeenCalledWith(expect.stringMatching(/\/login$/)));
    expect(api.logout).toHaveBeenCalledOnce();
  });
});
