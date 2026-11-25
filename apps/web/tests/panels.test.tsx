import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { BulkImport } from "@/components/BulkImport";
import { CollectionSetup } from "@/components/CollectionSetup";
import type { DiscoveryRun } from "@/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listScopes: vi.fn(async () => []),
    listDiscoveryRuns: vi.fn(),
  };
});

const api = await import("@/lib/api");

function run(id: string): DiscoveryRun {
  return {
    id,
    status: "frozen",
    started_at: "2026-11-20T10:00:00Z",
    frozen_at: "2026-11-20T11:00:00Z",
    degraded_reason: null,
    connectors: [
      {
        connector_id: "pubmed",
        required: true,
        outcome: "succeeded",
        records_seen: 2000,
        declared_total: 2000,
        accepted_degraded: false,
      },
    ],
    metrics: {},
  };
}

/**
 * A discovery run's card is tall — every connector with its counts — and the list
 * is read to compare a run with the one before it. Two a page, and no menu to
 * change that: the number is the point, not a preference.
 */
describe("The discovery runs panel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // The fold is remembered per browser; each case starts unfolded.
    window.localStorage.clear();
  });

  it("asks for two runs a page and offers no rows-per-page menu", async () => {
    vi.mocked(api.listDiscoveryRuns).mockResolvedValue({
      items: [run("run-aaaa1111"), run("run-bbbb2222")],
      next_cursor: "more",
      total: 7,
    });

    render(<CollectionSetup />);

    await waitFor(() =>
      expect(api.listDiscoveryRuns).toHaveBeenCalledWith(expect.objectContaining({ limit: 2 })),
    );
    expect(await screen.findByText("run-aaaa")).toBeInTheDocument();
    expect(screen.getByText("run-bbbb")).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: /Rows per page of discovery runs/ })).toBeNull();
    expect(screen.getByRole("button", { name: "Next page of discovery runs" })).toBeInTheDocument();
  });

  it("folds away to its title bar", async () => {
    vi.mocked(api.listDiscoveryRuns).mockResolvedValue({
      items: [run("run-aaaa1111")],
      next_cursor: null,
      total: 1,
    });
    const user = userEvent.setup();
    render(<CollectionSetup />);

    const fold = await screen.findByRole("button", { name: "Hide Discovery runs" });
    expect(fold).toHaveAttribute("aria-expanded", "true");
    await user.click(fold);

    expect(screen.getByRole("button", { name: "Show Discovery runs" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });
});

describe("The import panel", () => {
  beforeEach(() => window.localStorage.clear());

  it("folds away to its title bar", async () => {
    const user = userEvent.setup();
    render(<BulkImport waiting={[]} onImported={() => undefined} />);

    const fold = screen.getByRole("button", { name: "Hide Import many PDFs" });
    await user.click(fold);

    expect(screen.getByRole("button", { name: "Show Import many PDFs" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });
});
