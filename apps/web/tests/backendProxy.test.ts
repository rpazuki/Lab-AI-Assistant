import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GET, POST } from "@/app/api/backend/[...path]/route";

/** The `[...path]` segment, as Next hands it to the handler. */
const context = (...path: string[]) => ({ params: Promise.resolve({ path }) });

/** An undici failure, which carries the reason on `cause.code`. */
function networkError(code: string): Error {
  const error = new TypeError("fetch failed");
  (error as Error & { cause?: { code: string } }).cause = { code };
  return error;
}

/**
 * A throw inside a route handler becomes an HTML 500, which the typed client
 * cannot read as problem details: the browser is left with a bare status line.
 * The proxy answers in the client's own shape instead.
 */
describe("The proxy to the API, when the API does not answer", () => {
  beforeEach(() => vi.restoreAllMocks());
  afterEach(() => vi.restoreAllMocks());

  it("says the API is restarting when the connection is refused", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(networkError("ECONNREFUSED"));

    const response = await GET(new Request("http://app/api/backend/scopes"), context("scopes"));

    expect(response.status).toBe(502);
    const problem = await response.json();
    expect(problem.error_class).toBe("source_unavailable");
    expect(problem.detail).toMatch(/restarting after an update/);
  });

  it("says the work may still be running when it timed out", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(networkError("UND_ERR_HEADERS_TIMEOUT"));

    const response = await POST(
      new Request("http://app/api/backend/scopes/sv-1/delete-scope", { method: "POST" }),
      context("scopes", "sv-1", "delete-scope"),
    );

    expect(response.status).toBe(504);
    const problem = await response.json();
    expect(problem.error_class).toBe("source_unavailable");
    expect(problem.detail).toMatch(/may still be running/);
    expect(problem.detail).not.toMatch(/restarting/);
  });

  it("passes a real answer straight through", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ items: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    const response = await GET(new Request("http://app/api/backend/scopes"), context("scopes"));

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ items: [] });
  });

  it("passes the API's own refusal through rather than dressing it as unreachable", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error_class: "policy_blocked", detail: "not allowed" }), {
        status: 403,
        headers: { "content-type": "application/json" },
      }),
    );

    const response = await GET(new Request("http://app/api/backend/scopes"), context("scopes"));

    expect(response.status).toBe(403);
    expect((await response.json()).error_class).toBe("policy_blocked");
  });
});
