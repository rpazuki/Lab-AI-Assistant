import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CollectionSetup } from "@/components/CollectionSetup";
import { extractionRunDeletable } from "@/components/DatasheetsClient";
import { DeleteButton } from "@/components/ui/DeleteButton";
import type { DiscoveryRun, ExtractionRun } from "@/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listScopes: vi.fn(async () => []),
    listDiscoveryRuns: vi.fn(async () => ({ items: [], next_cursor: null, total: 0 })),
    deleteDiscoveryRun: vi.fn(async () => undefined),
  };
});

const api = await import("@/lib/api");

/** A one-page answer from a paged endpoint. */
const page = <T,>(items: T[]) => ({ items, next_cursor: null, total: items.length });

function run(id: string, status: string, records: number): DiscoveryRun {
  return {
    id,
    status,
    started_at: "2026-11-20T10:00:00Z",
    frozen_at: status === "frozen" ? "2026-11-20T11:00:00Z" : null,
    degraded_reason: null,
    connectors: [
      {
        connector_id: "pubmed",
        required: true,
        outcome: "succeeded",
        records_seen: records,
        declared_total: records,
        accepted_degraded: false,
      },
    ],
    metrics: {},
  };
}

describe("Delete is offered only where the server would allow it", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(() => vi.restoreAllMocks());

  it("offers it for stopped runs and frozen runs that found nothing", async () => {
    vi.mocked(api.listDiscoveryRuns).mockResolvedValue(page([
      run("degraded-run", "degraded", 2000),
      run("running-run", "running", 10),
      run("frozen-full", "frozen", 3130),
      run("frozen-empty", "frozen", 0),
    ]));
    render(<CollectionSetup />);
    expect(await screen.findByRole("button", { name: "Delete discovery run degraded" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete discovery run frozen-e" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete discovery run running-" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete discovery run frozen-f" })).toBeNull();
  });

  it("asks first, then deletes and reloads", async () => {
    vi.mocked(api.listDiscoveryRuns).mockResolvedValue(page([run("degraded-run", "degraded", 5)]));
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<CollectionSetup />);
    await user.click(await screen.findByRole("button", { name: "Delete discovery run degraded" }));
    expect(confirm.mock.calls[0]![0]).toMatch(/cannot be undone/);
    await waitFor(() => expect(api.deleteDiscoveryRun).toHaveBeenCalledWith("degraded-run"));
    expect(await screen.findByText("Discovery run degraded deleted.")).toBeInTheDocument();
  });

  it("does nothing when the question is declined", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const onDelete = vi.fn();
    const user = userEvent.setup();
    render(<DeleteButton what="thing" consequence="Gone." onDelete={onDelete} />);
    await user.click(screen.getByRole("button", { name: "Delete thing" }));
    expect(onDelete).not.toHaveBeenCalled();
  });

  it("offers an extraction run only when every batch failed or was cancelled", () => {
    const base: ExtractionRun = {
      run_id: "r",
      template: "t@1",
      states: {},
      total: 2,
      finished: true,
      created_at: null,
      errors: [],
    };
    expect(extractionRunDeletable({ ...base, states: { dead_letter: 1, cancelled: 1 } })).toBe(true);
    expect(extractionRunDeletable({ ...base, states: { failed: 1, succeeded: 1 } })).toBe(false);
    expect(extractionRunDeletable({ ...base, finished: false, states: { failed: 1, ready: 1 } })).toBe(false);
  });
});
