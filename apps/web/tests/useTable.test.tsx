import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { SEARCH_THRESHOLD, SortHeader, TableSearch } from "@/components/ui/table";
import { compareValues, useTable } from "@/lib/useTable";

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

describe("every data table", () => {
  it("sorts through the shared headers", () => {
    // A table that lists rows uses SortHeader, so sorting is the same everywhere.
    // The help guides' status tables keep the glossary's order on purpose, and a
    // table in an answer is the model's text, shown in the order it wrote it.
    const dir = join(__dirname, "../src/components");
    const files = (function walk(path: string): string[] {
      return readdirSync(path).flatMap((name) => {
        const full = join(path, name);
        return statSync(full).isDirectory() ? walk(full) : full.endsWith(".tsx") ? [full] : [];
      });
    })(dir);
    const unsorted = files.filter((file) => {
      const text = readFileSync(file, "utf8");
      return /<table/.test(text) && !text.includes("<SortHeader") && !file.endsWith("HelpClient.tsx") &&
        !file.endsWith("Markdown.tsx")
    });
    expect(unsorted).toEqual([]);
  });
});
