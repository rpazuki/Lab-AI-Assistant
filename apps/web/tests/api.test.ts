import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, UNREACHABLE, ask, getAbstractFallback, login, parseFrame, search, setRelevance } from "@/lib/api";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("API client", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts credentials to the same-origin login route", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ workspaces: [{ id: "1", slug: "lab", name: "Lab" }] }));
    const body = await login("s@lab.invalid", "passphrase");
    expect(body.workspaces[0]?.slug).toBe("lab");
    const [url, init] = vi.mocked(fetch).mock.calls[0]!;
    expect(url).toBe("/assist/api/auth/login");
    expect(init?.method).toBe("POST");
    // The token is never handled here: the route sets an httpOnly cookie.
    expect(JSON.stringify(init?.body)).not.toContain("access_token");
  });

  it("says in words when the server could not be reached, trying a read once more", async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(fetch).mockRejectedValueOnce(new TypeError("Failed to fetch"));
      vi.mocked(fetch).mockResolvedValueOnce(
        jsonResponse({ enabled: false, set_at: null, set_by: null, partial: 0, eligible: 0 }),
      );
      const read = getAbstractFallback();
      await vi.advanceTimersByTimeAsync(1500);
      expect((await read).enabled).toBe(false);
      expect(fetch).toHaveBeenCalledTimes(2);

      vi.mocked(fetch).mockReset();
      vi.mocked(fetch).mockRejectedValue(new TypeError("Failed to fetch"));
      // A change is not sent twice: the first may have arrived.
      await expect(setRelevance("w1", "included", "reviewer_in_scope")).rejects.toThrow(UNREACHABLE);
      expect(fetch).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("surfaces a problem document as an ApiError with its error class", async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(
        {
          type: "about:blank",
          title: "policy blocked",
          status: 403,
          detail: "missing capability manage_scope",
          error_class: "policy_blocked",
        },
        403,
      ),
    );
    await expect(setRelevance("w1", "included", "steward")).rejects.toMatchObject({
      name: "ApiError",
      status: 403,
      errorClass: "policy_blocked",
    });
  });

  it("does not lose the error when the body is not JSON", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response("gateway timeout", { status: 504 }));
    const caught = await search("lipid").catch((error) => error as ApiError);
    expect(caught).toBeInstanceOf(ApiError);
    expect((caught as ApiError).status).toBe(504);
  });

  it("routes every call through the same-origin proxy", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ results: [], release_id: "r", classification: "evidence", withheld_by_policy: 0 }));
    await search("lipid yield");
    expect(vi.mocked(fetch).mock.calls[0]![0]).toBe("/assist/api/backend/search");
  });

  describe("server-sent event parsing", () => {
    it("keeps token payloads verbatim", () => {
      expect(parseFrame("event: token\ndata:  and the yield")).toEqual({
        type: "token",
        data: " and the yield",
      });
    });

    it("parses structured events", () => {
      const frame = 'event: done\ndata: {"message_id":"m1","citations":[],"unsupported_handles":[]}';
      expect(parseFrame(frame)).toMatchObject({ type: "done" });
    });

    it("ignores a frame with no event name", () => {
      expect(parseFrame("data: orphaned")).toBeNull();
    });

    it("ignores a structured frame with broken JSON rather than throwing", () => {
      expect(parseFrame("event: done\ndata: {broken")).toBeNull();
    });
  });

  it("streams sources, tokens, and done in order", async () => {
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        const encoder = new TextEncoder();
        controller.enqueue(
          encoder.encode('event: sources\ndata: {"release_id":"r1","classification":"evidence","sources":[],"claims":[]}\n\n'),
        );
        controller.enqueue(encoder.encode("event: token\ndata: Strain\n\n"));
        // A frame split across chunks must still parse.
        controller.enqueue(encoder.encode("event: token\ndata:  YL-A"));
        controller.enqueue(encoder.encode("\n\nevent: done\ndata: {\"message_id\":\"m1\",\"citations\":[],\"unsupported_handles\":[],\"citation_validation_passed\":true,\"release_id\":\"r1\",\"latency_ms\":12}\n\n"));
        controller.close();
      },
    });
    vi.mocked(fetch).mockResolvedValue(new Response(stream, { status: 200 }));

    const events: string[] = [];
    let answer = "";
    await ask("s1", "question", (event) => {
      events.push(event.type);
      if (event.type === "token") answer += event.data;
    });
    expect(events).toEqual(["sources", "token", "token", "done"]);
    expect(answer).toBe("Strain YL-A");
  });

  it("raises when the stream cannot start", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(null, { status: 502 }));
    await expect(ask("s1", "q", () => {})).rejects.toBeInstanceOf(ApiError);
  });
});
