import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NavBar } from "@/components/NavBar";
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
  screen.queryAllByRole("link").map((link) => link.textContent).filter((t) => t !== "Lab AI Assistant");

describe("NavBar", () => {
  beforeEach(() => {
    vi.clearAllMocks();
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
    render(<NavBar />);
    expect(await screen.findByRole("link", { name: "Sign in" })).toHaveAttribute("aria-current", "page");
    expect(sections()).toEqual(["Sign in"]);
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
  });

  it("shows no section while it is still asking who is signed in", () => {
    vi.mocked(api.getMe).mockReturnValue(new Promise(() => undefined));
    render(<NavBar />);
    expect(sections()).toEqual([]);
  });

  it("gives a researcher only the sections a researcher can use", async () => {
    vi.mocked(api.getMe).mockResolvedValue(
      member("researcher", ["ask", "search", "upload_asset", "view_analytics"]),
    );
    render(<NavBar />);
    expect(await screen.findByRole("button", { name: "Sign out" })).toBeInTheDocument();
    expect(sections()).toEqual(["Ask", "Analytics"]);
  });

  it("gives a reviewer the review queue but not releases", async () => {
    vi.mocked(api.getMe).mockResolvedValue(
      member("reviewer", ["ask", "search", "upload_asset", "view_analytics", "decide_identity", "review_parse", "review_claims"]),
    );
    render(<NavBar />);
    await screen.findByRole("button", { name: "Sign out" });
    expect(sections()).toEqual(["Ask", "Collections", "Review", "Analytics"]);
  });

  it("gives an admin every section", async () => {
    vi.mocked(api.getMe).mockResolvedValue(member("admin", ALL));
    render(<NavBar />);
    await screen.findByRole("button", { name: "Sign out" });
    expect(sections()).toEqual(["Ask", "Collections", "Review", "Releases", "Analytics"]);
    expect(screen.getByRole("link", { name: "Ask" })).toHaveAttribute("aria-current", "page");
  });

  it("signs out and reloads on the sign-in page", async () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    vi.mocked(api.getMe).mockResolvedValue(member("admin", ALL));
    render(<NavBar />);
    (await screen.findByRole("button", { name: "Sign out" })).click();
    await waitFor(() => expect(assign).toHaveBeenCalledWith(expect.stringMatching(/\/login$/)));
    expect(api.logout).toHaveBeenCalledOnce();
  });
});
