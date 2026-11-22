import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { HelpArticle } from "@/components/HelpClient";
import { Panel, StateChip } from "@/components/ui/primitives";
import { HELP_TOPICS, helpTopic, searchHelp } from "@/lib/help";
import { STATUS_KINDS, describeStatus, statusGroup } from "@/lib/statuses";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, getMe: vi.fn(async () => ({ capabilities: [] })) };
});

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? sources(path) : path.endsWith(".tsx") ? [path] : [];
  });
}

describe("the status glossary", () => {
  it("describes the discovery outcomes a source can end in", () => {
    for (const value of ["degraded_partial", "source_unavailable_retryable", "authentication_required"]) {
      expect(describeStatus("source_outcome", value)).toBeTruthy();
    }
  });

  it("is tabulated in some guide, group by group", () => {
    const shown = new Set(HELP_TOPICS.flatMap((topic) => topic.sections.flatMap((s) => s.statuses ?? [])));
    expect(STATUS_KINDS.filter((kind) => !shown.has(kind))).toEqual([]);
  });

  it("is explained on every status chip in the app", () => {
    // A chip without a kind shows no explanation. Gate results (name=pass) are
    // composite labels, not statuses, and are the one deliberate exception.
    const missing = sources(join(__dirname, "../src"))
      .flatMap((file) =>
        readFileSync(file, "utf8")
          .split("\n")
          .filter((line) => line.includes("<StateChip state=") && !line.includes("kind="))
          .map((line) => `${file}: ${line.trim()}`),
      )
      .filter((line) => !/pass|fail/.test(line) && !line.includes("HelpClient.tsx"));
    expect(missing).toEqual([]);
  });

  it("finds a section from a status name in search", () => {
    const found = searchHelp("degraded_partial");
    expect(found.map((match) => match.section.id)).toContain("discovery");
  });
});

describe("the info button beside a status", () => {
  it("shows what the status means when clicked, and closes on Escape", async () => {
    const user = userEvent.setup();
    render(
      <Panel title="Runs">
        <StateChip state="degraded_partial" kind="source_outcome" />
      </Panel>,
    );
    const button = screen.getByRole("button", { name: "What “degraded_partial” means" });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();

    await user.click(button);
    const tip = screen.getByRole("tooltip");
    expect(tip).toHaveTextContent(describeStatus("source_outcome", "degraded_partial")!);
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(button).toHaveAttribute("aria-describedby", tip.id);
    // Drawn outside the card, so overflow-hidden cannot clip it.
    expect(tip.closest("section")).toBeNull();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    expect(button).toHaveFocus();
  });

  it("closes when clicking elsewhere", async () => {
    const user = userEvent.setup();
    render(
      <div>
        <StateChip state="running" kind="discovery_run" />
        <p>elsewhere</p>
      </div>,
    );
    await user.click(screen.getByRole("button", { name: "What “running” means" }));
    expect(screen.getByRole("tooltip")).toBeInTheDocument();
    await user.click(screen.getByText("elsewhere"));
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("is absent when the value has no description", () => {
    render(<StateChip state="something_new" kind="source_outcome" />);
    expect(screen.getByText("something_new")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("a guide's status tables", () => {
  it("lists every value of the section's groups with its meaning", () => {
    render(<HelpArticle topic={helpTopic("collections")!} />);
    const table = screen.getByRole("table", { name: statusGroup("source_outcome").title });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(Object.keys(statusGroup("source_outcome").values).length);
    expect(within(table).getByText("source_unavailable_retryable")).toBeInTheDocument();
    expect(
      within(table).getByText(describeStatus("source_outcome", "authentication_required")!),
    ).toBeInTheDocument();
  });
});
