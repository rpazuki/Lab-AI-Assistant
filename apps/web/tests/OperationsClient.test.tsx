import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { OperationsClient } from "@/components/OperationsClient";
import type { AuditEvent, QueueTask } from "@/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    taskSnapshot: vi.fn(),
    taskQueue: vi.fn(),
    listAuditEvents: vi.fn(),
    retryTask: vi.fn(),
  };
});

const api = await import("@/lib/api");

function task(index: number, stage = "reconciliation"): QueueTask {
  return {
    task_id: `t${String(index).padStart(7, "0")}x`,
    stage,
    state: "ready",
    created_at: new Date(Date.UTC(2026, 10, 1, 0, index)).toISOString(),
    attempt: 0,
    action: null,
    error: null,
  };
}

function event(index: number, action = "login_succeeded"): AuditEvent {
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

const EMPTY = { items: [], next_cursor: null, total: 0 };

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.taskSnapshot).mockResolvedValue({
    counts: { ready: 23 },
    oldest_pending_by_stage: {},
    waiting_by_stage: [
      { stage: "reconciliation", count: 20, oldest: "2026-11-01T00:00:00Z" },
      { stage: "acquisition", count: 3, oldest: "2026-11-02T00:00:00Z" },
    ],
    awaiting_user: [],
    dead_letters: [],
  });
  vi.mocked(api.taskQueue).mockImplementation(async (queue, options) => {
    if (queue !== "waiting") return EMPTY;
    if (options?.stage === "acquisition") {
      return { items: [task(1, "acquisition")], next_cursor: null, total: 3 };
    }
    return options?.after
      ? { items: [task(10), task(11)], next_cursor: null, total: 12 }
      : { items: Array.from({ length: 10 }, (_, i) => task(i)), next_cursor: "c10", total: 12 };
  });
  vi.mocked(api.listAuditEvents).mockImplementation(async (options) => ({
    // Newest first from the API; a search narrows on the server.
    items: options?.q ? [event(3, "scope_approved")] : [event(13), event(12)],
    next_cursor: options?.q ? null : "a12",
    total: options?.q ? 1 : 140,
    action_counts: { login_succeeded: 139, scope_approved: 1 },
  }));
});

describe("the operations page", () => {
  it("pages the waiting tasks and narrows them to a stage", async () => {
    const user = userEvent.setup();
    render(<OperationsClient />);
    expect(await screen.findByText("1–10 of 12 waiting tasks")).toBeInTheDocument();
    expect(api.taskQueue).toHaveBeenCalledWith("waiting", { stage: null, after: null, limit: 10, q: null });

    await user.click(screen.getByRole("button", { name: "Next page of waiting tasks" }));
    expect(await screen.findByText("11–12 of 12 waiting tasks")).toBeInTheDocument();
    expect(api.taskQueue).toHaveBeenCalledWith("waiting", { stage: null, after: "c10", limit: 10, q: null });

    // Choosing a stage starts again at its first page.
    const stage = screen.getByRole("combobox", { name: "Waiting tasks in stage" });
    expect(within(stage).getByRole("option", { name: /acquisition \(3, oldest/ })).toBeInTheDocument();
    await user.selectOptions(stage, "acquisition");
    await waitFor(() =>
      expect(api.taskQueue).toHaveBeenCalledWith("waiting", { stage: "acquisition", after: null, limit: 10, q: null }),
    );
    expect(await screen.findByText("3 waiting tasks")).toBeInTheDocument();
  });

  it("gives every task list a When column and a search the server runs, dates included", async () => {
    const user = userEvent.setup();
    render(<OperationsClient />);
    await screen.findByText("1–10 of 12 waiting tasks");
    const waiting = screen.getAllByRole("table")[0]!;
    expect(within(waiting).getByRole("columnheader", { name: /When/ })).toBeInTheDocument();
    expect(within(waiting).getAllByText("2026-11-01 00:00 UTC").length).toBeGreaterThan(0);

    await user.type(screen.getByRole("searchbox", { name: "Search dead letters" }), "2026-11-01{Enter}");
    await waitFor(() =>
      expect(api.taskQueue).toHaveBeenCalledWith("dead_letter", {
        stage: null,
        after: null,
        limit: 10,
        q: "2026-11-01",
      }),
    );
    expect(screen.getByRole("searchbox", { name: "Search waiting tasks" })).toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: "Search tasks waiting for a person" })).toBeInTheDocument();
  });

  it("asks the server for each page of the audit log and searches every event", async () => {
    const user = userEvent.setup();
    render(<OperationsClient />);
    expect(await screen.findByText("1–2 of 140 audit events")).toBeInTheDocument();
    expect(api.listAuditEvents).toHaveBeenCalledWith({ after: null, limit: 50, action: null, q: null });
    const audit = screen.getAllByRole("table").at(-1)!;
    expect(within(audit).getAllByRole("row")[1]).toHaveTextContent("s13");

    await user.click(screen.getByRole("button", { name: "Next page of audit events" }));
    await waitFor(() =>
      expect(api.listAuditEvents).toHaveBeenCalledWith({ after: "a12", limit: 50, action: null, q: null }),
    );

    // A search goes to the server, over every event, from the first page.
    await user.type(screen.getByRole("searchbox", { name: "Search audit events" }), "approved{Enter}");
    await waitFor(() =>
      expect(api.listAuditEvents).toHaveBeenCalledWith({ after: null, limit: 50, action: null, q: "approved" }),
    );
    expect(await screen.findByText("1 audit events")).toBeInTheDocument();

    await user.selectOptions(screen.getByRole("combobox", { name: "Filter by action" }), "scope_approved");
    await waitFor(() =>
      expect(api.listAuditEvents).toHaveBeenCalledWith({
        after: null,
        limit: 50,
        action: "scope_approved",
        q: "approved",
      }),
    );
  });
});

/**
 * Four long lists on one page. Each folds away to its title bar and remembers it,
 * so an operator reading dead letters is not scrolling past the audit log to do
 * it. "Waiting longest" and "Waiting for a person" are one above the other now:
 * side by side each had half a screen for a stage, a state, a time and an action.
 */
describe("Every panel on Operations folds away", () => {
  // The fold is remembered per browser, so one case's folding would otherwise
  // start the next one folded.
  beforeEach(() => window.localStorage.clear());

  it("offers each one its own fold, and remembers it", async () => {
    const user = userEvent.setup();
    render(<OperationsClient />);

    for (const title of ["Waiting longest", "Waiting for a person", "Dead letters", "Audit log"]) {
      const fold = await screen.findByRole("button", { name: `Hide ${title}` });
      expect(fold).toHaveAttribute("aria-expanded", "true");
      await user.click(fold);
      expect(screen.getByRole("button", { name: `Show ${title}` })).toHaveAttribute(
        "aria-expanded",
        "false",
      );
    }
  });

  it("stacks the two waiting panels rather than setting them side by side", async () => {
    render(<OperationsClient />);

    const longest = (await screen.findByRole("button", { name: "Hide Waiting longest" })).closest(
      "section",
    );
    const person = screen.getByRole("button", { name: "Hide Waiting for a person" }).closest(
      "section",
    );
    // Siblings in the page's own column, not two cells of a row.
    expect(longest?.parentElement).toBe(person?.parentElement);
    expect(longest?.parentElement?.className ?? "").not.toMatch(/grid-cols-2/);
  });
});
