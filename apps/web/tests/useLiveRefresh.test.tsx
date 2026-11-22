import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LiveStatus } from "@/components/ui/LiveStatus";
import { LIVE_INTERVAL_MS, SETTLE_MS, useLiveRefresh } from "@/lib/useLiveRefresh";

function Harness({
  load,
  active,
  idleMs,
  onKick,
}: {
  load: () => Promise<void>;
  active: boolean;
  idleMs?: number | null;
  onKick?: (kick: () => void) => void;
}) {
  const live = useLiveRefresh(load, { active, idleMs });
  onKick?.(live.kick);
  return <LiveStatus {...live} />;
}

const tick = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms));

describe("useLiveRefresh", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("asks again on the live interval while something is in progress", async () => {
    const load = vi.fn(async () => undefined);
    render(<Harness load={load} active />);
    await tick(0);
    expect(load).toHaveBeenCalledTimes(1);
    await tick(LIVE_INTERVAL_MS * 2 + 10);
    expect(load).toHaveBeenCalledTimes(3);
    expect(screen.getByText(/Updating live/)).toBeInTheDocument();
  });

  it("stops asking when nothing is in progress", async () => {
    const load = vi.fn(async () => undefined);
    render(<Harness load={load} active={false} />);
    await tick(LIVE_INTERVAL_MS * 3);
    expect(load).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/^Updated /)).toBeInTheDocument();
  });

  it("keeps asking for the settle window after a change, then stops", async () => {
    const load = vi.fn(async () => undefined);
    let kick = () => {};
    render(<Harness load={load} active={false} onKick={(next) => (kick = next)} />);
    await tick(0);
    act(() => kick());
    await tick(LIVE_INTERVAL_MS + 10);
    expect(load.mock.calls.length).toBeGreaterThanOrEqual(3);
    expect(screen.getByText(/Updating live/)).toBeInTheDocument();

    await tick(SETTLE_MS);
    const settled = load.mock.calls.length;
    expect(screen.getByText(/^Updated /)).toBeInTheDocument();
    await tick(LIVE_INTERVAL_MS * 3);
    expect(load).toHaveBeenCalledTimes(settled);
  });

  it("keeps a slow background interval when one is given", async () => {
    const load = vi.fn(async () => undefined);
    render(<Harness load={load} active={false} idleMs={15_000} />);
    await tick(15_010);
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("does not ask while the tab is hidden, and asks as soon as it is shown", async () => {
    const load = vi.fn(async () => undefined);
    const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    render(<Harness load={load} active />);
    await tick(LIVE_INTERVAL_MS * 3);
    expect(load).toHaveBeenCalledTimes(1);

    hidden.mockReturnValue(false);
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await tick(0);
    expect(load).toHaveBeenCalledTimes(2);
    hidden.mockRestore();
  });

  it("never runs two loads at once", async () => {
    let finish = () => {};
    const load = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
    );
    render(<Harness load={load} active />);
    await tick(LIVE_INTERVAL_MS * 3);
    expect(load).toHaveBeenCalledTimes(1);
    await act(async () => finish());
    await tick(LIVE_INTERVAL_MS + 10);
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("loads a changed question after the one in flight, never alongside it", async () => {
    let finishFirst = () => {};
    const first = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finishFirst = resolve;
        }),
    );
    const second = vi.fn(async () => undefined);
    let refresh: () => Promise<void> = async () => {};
    function Paged({ load }: { load: () => Promise<void> }) {
      const live = useLiveRefresh(load, { active: false });
      refresh = live.refresh;
      return null;
    }
    const { rerender } = render(<Paged load={first} />);
    await tick(0);
    expect(first).toHaveBeenCalledTimes(1);
    // The next page is asked for while the first page is still loading.
    rerender(<Paged load={second} />);
    act(() => void refresh());
    await tick(10);
    expect(second).not.toHaveBeenCalled();
    await act(async () => finishFirst());
    await tick(10);
    expect(second).toHaveBeenCalledTimes(1);
  });
});
