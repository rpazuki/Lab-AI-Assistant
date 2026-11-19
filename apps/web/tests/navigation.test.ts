import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";

import { sessionCookie } from "@/lib/config";
import { safeNext } from "@/lib/navigation";
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

  it.each([undefined, "", "review", "//evil.example", "/\\evil.example", "https://evil.example", "/login", "/login?next=/ask"])(
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
