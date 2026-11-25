import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { COLLAPSED_KEY, OPEN_GROUPS_KEY, Sidebar, resetSidebarGroups } from "@/components/Sidebar";
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
    expect(groupHeaders()).toEqual(["Research", "Help"]);
  });

  it("gives a reviewer the review queue but not releases", async () => {
    vi.mocked(api.getMe).mockResolvedValue(
      member("reviewer", ["ask", "search", "upload_asset", "view_analytics", "decide_identity", "review_parse", "review_claims"]),
    );
    window.localStorage.setItem(OPEN_GROUPS_KEY, JSON.stringify(["research", "curation"]));
    render(<Sidebar />);
    await screen.findByRole("button", { name: "Sign out" });
    expect(sections()).toEqual(["Ask", "Analytics", "Collections", "Review", "Answer reviews"]);
  });

  it("gives an admin every section, grouped", async () => {
    window.localStorage.setItem(
      OPEN_GROUPS_KEY,
      JSON.stringify(["research", "curation", "publishing", "administration", "help"]),
    );
    vi.mocked(api.getMe).mockResolvedValue(member("admin", ALL));
    render(<Sidebar />);
    await screen.findByRole("button", { name: "Sign out" });
    expect(sections()).toEqual([
      "Ask", "Analytics", "Collections", "Collection progress", "Review", "Datasheets", "Answer reviews", "Releases",
      "Evaluation",
      "Members", "Operations", "How-to guides",
    ]);
    expect(groupHeaders()).toEqual(["Research", "Curation", "Publishing", "Administration", "Help"]);
    expect(screen.getByRole("link", { name: "Ask" })).toHaveAttribute("aria-current", "page");
  });

  it("opens only the current page's group on a first visit", async () => {
    pathname = "/evaluation";
    vi.mocked(api.getMe).mockResolvedValue(member("admin", ALL));
    render(<Sidebar />);
    await screen.findByRole("button", { name: "Sign out" });
    const expanded = (name: string) =>
      screen.getByRole("button", { name }).getAttribute("aria-expanded");
    expect(expanded("Publishing")).toBe("true");
    expect(
      [expanded("Research"), expanded("Curation"), expanded("Administration"), expanded("Help")],
    ).toEqual(["false", "false", "false", "false"]);
    expect(sections()).toEqual(["Releases", "Evaluation"]);
  });

  it("opens and closes a group, and remembers it", async () => {
    vi.mocked(api.getMe).mockResolvedValue(member("admin", ALL));
    render(<Sidebar />);
    const header = await screen.findByRole("button", { name: "Publishing" });
    expect(header).toHaveAttribute("aria-expanded", "false");

    await userEvent.click(header);
    expect(header).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("link", { name: "Releases" })).toBeInTheDocument();
    expect(JSON.parse(window.localStorage.getItem(OPEN_GROUPS_KEY) ?? "[]")).toEqual([
      "research",
      "publishing",
    ]);

    await userEvent.click(header);
    expect(screen.queryByRole("link", { name: "Releases" })).not.toBeInTheDocument();
  });

  it("opens the group of a page reached from elsewhere", async () => {
    window.localStorage.setItem(OPEN_GROUPS_KEY, JSON.stringify(["research"]));
    vi.mocked(api.getMe).mockResolvedValue(member("admin", ALL));
    const { rerender } = render(<Sidebar />);
    await screen.findByRole("button", { name: "Sign out" });
    pathname = "/members";
    rerender(<Sidebar />);
    expect(await screen.findByRole("link", { name: "Members" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Ask" })).toBeInTheDocument();
  });

  it("starts tidy again after signing in", async () => {
    window.localStorage.setItem(
      OPEN_GROUPS_KEY,
      JSON.stringify(["research", "curation", "publishing", "administration"]),
    );
    vi.mocked(api.getMe).mockResolvedValue(member("admin", ALL));
    render(<Sidebar />);
    await screen.findByRole("link", { name: "Members" });
    act(() => resetSidebarGroups());
    expect(window.localStorage.getItem(OPEN_GROUPS_KEY)).toBeNull();
    await waitFor(() => expect(sections()).toEqual(["Ask", "Analytics"]));
  });

  it("hides down to a rail of icons that keep their names", async () => {
    vi.mocked(api.getMe).mockResolvedValue(member("admin", ALL));
    render(<Sidebar />);
    await screen.findByRole("button", { name: "Sign out" });

    await userEvent.click(screen.getByRole("button", { name: "Hide navigation" }));
    const show = screen.getByRole("button", { name: "Show navigation" });
    expect(show).toHaveAttribute("aria-expanded", "false");
    expect(window.localStorage.getItem(COLLAPSED_KEY)).toBe("1");
    // No group headers in the rail, but every section is still a named link, and
    // each icon has a tooltip with its name for the mouse and the keyboard.
    expect(groupHeaders()).toEqual([]);
    const members = screen.getByRole("link", { name: "Members" });
    expect(members).not.toHaveAttribute("title");
    const tip = members.querySelector("[data-tooltip]");
    expect(tip).toHaveTextContent("Members");
    expect(tip).toHaveAttribute("aria-hidden");
    expect(tip?.className).toMatch(/group-hover:opacity-100/);
    expect(tip?.className).toMatch(/group-focus-visible:opacity-100/);
    expect(
      screen.getByRole("button", { name: "Show navigation" }).querySelector("[data-tooltip]"),
    ).toHaveTextContent("Show navigation");
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
