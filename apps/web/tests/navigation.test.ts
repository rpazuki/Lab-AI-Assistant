import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";

import { sessionCookie } from "@/lib/config";
import { NAV, safeNext, visibleGroups } from "@/lib/navigation";
import { config, middleware } from "@/middleware";

function page(path: string, cookie?: string): NextRequest {
  return new NextRequest(`http://127.0.0.1:3002${path}`, {
    headers: cookie ? { cookie } : {},
    nextConfig: { basePath: "/assist" },
  });
}

describe("safeNext", () => {
  it("keeps a path inside the app", () => {
    expect(safeNext("/review?claim=1")).toBe("/review?claim=1");
  });

  it.each([undefined, "", "review", "//evil.example", "/\\evil.example", "https://evil.example", "/login", "/login?next=/ask", "/invite"])(
    "sends %j home instead",
    (next) => {
      expect(safeNext(next)).toBe("/ask");
    },
  );
});

describe("middleware", () => {
  it("sends a visitor without a session to sign in, remembering the page", () => {
    const response = middleware(page("/assist/releases?tab=1"));
    expect(response.status).toBe(307);
    expect(response.headers.get("location")).toMatch(/\/assist\/login\?next=%2Freleases%3Ftab%3D1$/);
  });

  it("sends the bare address to sign in with nothing to remember", () => {
    const response = middleware(page("/assist"));
    expect(response.headers.get("location")).toMatch(/\/assist\/login$/);
  });

  it("lets the sign-in page through", () => {
    expect(middleware(page("/assist/login")).headers.get("location")).toBeNull();
  });

  it("lets the invitation page through, since its visitor has no account yet", () => {
    expect(middleware(page("/assist/invite")).headers.get("location")).toBeNull();
  });

  it("lets a member with a session through", () => {
    const response = middleware(page("/assist/review", `${sessionCookie}=token`));
    expect(response.headers.get("location")).toBeNull();
  });

  it("leaves route handlers and assets alone", () => {
    const [pattern] = config.matcher;
    const matches = (path: string) => new RegExp(`^${pattern}$`).test(path);
    expect(matches("/ask")).toBe(true);
    expect(matches("/")).toBe(true);
    expect(matches("/api/backend/auth/me")).toBe(false);
    expect(matches("/_next/static/chunk.js")).toBe(false);
    expect(matches("/favicon.ico")).toBe(false);
  });
});

describe("visibleGroups", () => {
  it("drops a group with nothing the member may open", () => {
    const groups = visibleGroups(["ask", "view_operations"]);
    // Help is for everyone, so it is always there.
    expect(groups.map((group) => group.id)).toEqual(["research", "administration", "help"]);
    expect(groups[1]?.items.map((item) => item.label)).toEqual(["Operations"]);
  });

  it("names every section with a distinct path", () => {
    expect(new Set(NAV.map((item) => item.href)).size).toBe(NAV.length);
  });
});

describe("an item kept for one kind of installation", () => {
  const admin = ["manage_policy", "manage_members"];
  const labels = (groups: ReturnType<typeof visibleGroups>) =>
    groups.flatMap((group) => group.items.map((item) => item.label));

  it("shows Mirror access to a mirror's admin", () => {
    expect(labels(visibleGroups(admin, "reader"))).toContain("Mirror access");
  });

  it("hides it on the publisher, and while the installation is not known", () => {
    expect(labels(visibleGroups(admin, "publisher"))).not.toContain("Mirror access");
    expect(labels(visibleGroups(admin))).not.toContain("Mirror access");
  });

  it("hides it from a mirror's member who is not an admin", () => {
    expect(labels(visibleGroups(["ask"], "reader"))).not.toContain("Mirror access");
  });
});
