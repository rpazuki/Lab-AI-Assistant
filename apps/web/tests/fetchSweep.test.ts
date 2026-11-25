import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { watchSweep } from "@/components/CollectionsClient";
import type { FetchSweepCounts } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, fetchSweep: vi.fn() };
});

const api = await import("@/lib/api");

const counts: FetchSweepCounts = {
  requeued: 1425,
  replanned: 300,
  kept_for_a_person: 6467,
  already_tried: 12,
  degraded_promoted: 40,
  still_degraded: 301,
};

const running = { task_id: "t1", state: "leased", settled: false, counts: null, error: null };
const done = { task_id: "t1", state: "succeeded", settled: true, counts, error: null };

/**
 * The sweep is a queued task now, so the page asks after it rather than holding
 * the request open. What it says when it ends — or does not end — is the whole
 * of what a person sees, so each ending has a test.
 */
describe("Watching a queued fetch sweep", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(api.fetchSweep).mockReset();
  });
  afterEach(() => vi.useRealTimers());

  /** Run the watch to its end without waiting ten real minutes for it. */
  async function settle(watching: Promise<string>): Promise<string> {
    await vi.advanceTimersByTimeAsync(2_000 * 320);
    return watching;
  }

  it("reports the counts once the sweep has settled", async () => {
    vi.mocked(api.fetchSweep).mockResolvedValueOnce(running).mockResolvedValue(done);

    const said = await settle(watchSweep("t1"));

    expect(said).toMatch(/1,425 fetches will be tried again, 300 of them papers/);
    expect(said).toMatch(/6,467 can only come from an upload/);
    expect(said).toMatch(/40 degraded documents now pass the parse limits/);
  });

  it("keeps asking through a failed poll rather than giving up on a durable task", async () => {
    vi.mocked(api.fetchSweep)
      .mockRejectedValueOnce(new Error("the API could not be reached"))
      .mockRejectedValueOnce(new Error("the API could not be reached"))
      .mockResolvedValue(done);

    expect(await settle(watchSweep("t1"))).toMatch(/1,425 fetches will be tried again/);
    expect(vi.mocked(api.fetchSweep).mock.calls.length).toBeGreaterThan(2);
  });

  it("passes on the reason when the sweep settled without counts", async () => {
    vi.mocked(api.fetchSweep).mockResolvedValue({
      ...running,
      state: "dead_letter",
      settled: true,
      error: "the connector ran out of retries",
    });

    expect(await settle(watchSweep("t1"))).toBe("the connector ran out of retries");
  });

  it("says a sweep still running is still running, not that it finished", async () => {
    vi.mocked(api.fetchSweep).mockResolvedValue(running);

    const said = await settle(watchSweep("t1"));

    expect(said).toMatch(/Still running/);
    expect(said).toMatch(/Collection progress/);
    expect(said).not.toMatch(/will be tried again/);
  });

  it("does not call a sweep nothing has claimed running", async () => {
    // Ten minutes still `ready` is the compute worker being down, not slow work:
    // sending someone to watch progress that is not being made wastes their time.
    vi.mocked(api.fetchSweep).mockResolvedValue({ ...running, state: "ready" });

    const said = await settle(watchSweep("t1"));

    expect(said).toMatch(/nothing has picked it up/);
    expect(said).toMatch(/Operations/);
    expect(said).not.toMatch(/Still running/);
  });
});
