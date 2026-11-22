import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { OperationsClient } from "@/components/OperationsClient";
import { SEARCH_THRESHOLD, SortHeader, TableSearch } from "@/components/ui/table";
import { compareValues, useTable } from "@/lib/useTable";
import type { AuditEvent } from "@/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    taskSnapshot: vi.fn(async () => ({
      counts: {},
      oldest_pending_by_stage: {},
      awaiting_user: [],
      dead_letters: [],
    })),
    listAuditEvents: vi.fn(),
    retryTask: vi.fn(),
  };
});

const api = await import("@/lib/api");

type Row = { name: string; size: string | null; at: string };

function Harness({ rows, timed = false }: { rows: Row[]; timed?: boolean }) {
  const table = useTable(rows, {
    columns: { name: (row) => row.name, size: (row) => row.size, at: (row) => row.at },
    defaultSort: timed ? { key: "at", direction: "desc" } : undefined,
    searchText: (row) => row.name,
  });
  return (
    <>
      <TableSearch table={table} label="rows" />
      <table>
        <thead>
          <tr>
            <SortHeader table={table} column="name">
              Name
            </SortHeader>
            <SortHeader table={table} column="size" numeric>
              Size
            </SortHeader>
            <SortHeader table={table} column="at">
              At
            </SortHeader>
          </tr>
        </thead>
        <tbody>
          {table.rows.map((row) => (
            <tr key={row.name}>
              <td>{row.name}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

const names = () =>
  screen
    .getAllByRole("row")
    .slice(1)
    .map((row) => row.textContent);

const ROWS: Row[] = [
  { name: "beta", size: "0.5 g/g", at: "2026-11-20T10:00:00Z" },
  { name: "alpha", size: null, at: "2026-11-22T09:00:00Z" },
  { name: "gamma", size: "0.42 g/g", at: "2026-11-21T08:00:00Z" },
  { name: "delta", size: "12 g/g", at: "2026-11-19T08:00:00Z" },
];

describe("compareValues", () => {
  it("orders measured values by their number, not their text", () => {
    expect(compareValues("0.42 g/g", "0.5 g/g")).toBeLessThan(0);
    expect(compareValues("12 g/g", "9 g/g")).toBeGreaterThan(0);
  });

  it("orders timestamps by time", () => {
    expect(compareValues("2026-11-22T09:00:00Z", "2026-11-22T10:00:00+02:00")).toBeGreaterThan(0);
  });

  it("puts empty values last", () => {
    expect(compareValues(null, "a")).toBeGreaterThan(0);
    expect(compareValues("a", "")).toBeLessThan(0);
  });
});

describe("a sortable table", () => {
  it("keeps the API's order until a header is selected, then sorts and reverses", async () => {
    const user = userEvent.setup();
    render(<Harness rows={ROWS} />);
    expect(names()).toEqual(["beta", "alpha", "gamma", "delta"]);

    const header = screen.getByRole("columnheader", { name: /Name/ });
    await user.click(within(header).getByRole("button"));
    expect(names()).toEqual(["alpha", "beta", "delta", "gamma"]);
    expect(header).toHaveAttribute("aria-sort", "ascending");

    await user.click(within(header).getByRole("button"));
    expect(names()).toEqual(["gamma", "delta", "beta", "alpha"]);
    expect(header).toHaveAttribute("aria-sort", "descending");
  });

  it("keeps empty values at the bottom in both directions", async () => {
    const user = userEvent.setup();
    render(<Harness rows={ROWS} />);
    const button = within(screen.getByRole("columnheader", { name: /Size/ })).getByRole("button");
    await user.click(button);
    expect(names()).toEqual(["gamma", "beta", "delta", "alpha"]);
    await user.click(button);
    expect(names()).toEqual(["delta", "beta", "gamma", "alpha"]);
  });

  it("starts a timed table newest first", () => {
    render(<Harness rows={ROWS} timed />);
    expect(names()).toEqual(["alpha", "gamma", "beta", "delta"]);
    expect(screen.getByRole("columnheader", { name: /At/ })).toHaveAttribute("aria-sort", "descending");
  });

  it("offers search only once a table is long, and filters on every word", async () => {
    const { unmount } = render(<Harness rows={ROWS} />);
    expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();
    unmount();

    const many = Array.from({ length: SEARCH_THRESHOLD + 2 }, (_, index) => ({
      name: `row ${index} ${index % 2 ? "odd" : "even"}`,
      size: null,
      at: "2026-11-22T09:00:00Z",
    }));
    const user = userEvent.setup();
    render(<Harness rows={many} />);
    await user.type(screen.getByRole("searchbox", { name: "Search rows" }), "odd 1");
    expect(names()).toEqual(["row 1 odd", "row 11 odd"]);
    expect(screen.getByText(`2 of ${many.length}`)).toBeInTheDocument();
  });
});

describe("the audit log", () => {
  function event(index: number, action: string): AuditEvent {
    return {
      id: `e${index}`,
      occurred_at: new Date(Date.UTC(2026, 10, 1 + index)).toISOString(),
      actor_type: "user",
      action,
      subject_type: "scope_version",
      subject_id: `s${index}`,
      reason_code: null,
      metadata: {},
      integrity_hash: "h",
    };
  }

  beforeEach(() => {
    vi.mocked(api.listAuditEvents).mockResolvedValue(
      // Oldest first from the API: the page must still show the newest on top.
      Array.from({ length: 14 }, (_, index) => event(index, index === 3 ? "scope_approved" : "login_succeeded")),
    );
  });

  it("shows the newest event first, and can be searched", async () => {
    const user = userEvent.setup();
    render(<OperationsClient />);
    const search = await screen.findByRole("searchbox", { name: "Search audit events" });
    const audit = screen.getAllByRole("table").at(-1)!;
    const first = within(audit).getAllByRole("row")[1]!;
    expect(first).toHaveTextContent("s13");
    expect(api.listAuditEvents).toHaveBeenCalledWith(500);

    await user.type(search, "scope_approved");
    const rows = within(audit).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent("s3");
  });
});

describe("every data table", () => {
  it("sorts through the shared headers", () => {
    // A table that lists rows uses SortHeader, so sorting is the same everywhere.
    // The help guides' status tables keep the glossary's order on purpose.
    const dir = join(__dirname, "../src/components");
    const files = (function walk(path: string): string[] {
      return readdirSync(path).flatMap((name) => {
        const full = join(path, name);
        return statSync(full).isDirectory() ? walk(full) : full.endsWith(".tsx") ? [full] : [];
      });
    })(dir);
    const unsorted = files.filter((file) => {
      const text = readFileSync(file, "utf8");
      return /<table/.test(text) && !text.includes("<SortHeader") && !file.endsWith("HelpClient.tsx");
    });
    expect(unsorted).toEqual([]);
  });
});
