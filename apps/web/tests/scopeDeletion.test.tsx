import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ScopeDeletion } from "@/components/ScopeReview";
import type { ScopeDeletionPlan, ScopeVersion } from "@/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, previewScopeDeletion: vi.fn(), deleteScope: vi.fn() };
});

const api = await import("@/lib/api");

const scope: ScopeVersion = {
  id: "sv-1",
  scope_id: "s-1",
  version: 2,
  name: "Marine toxicology",
  approval_state: "approved",
  connectors: ["pubmed"],
  scientific_objective: "o",
  concepts: [],
  exclusion_terms: [],
  relevance_policy: { include_threshold: 0.7, exclude_threshold: 0.3 },
  intended_uses: [],
  connector_plan: [],
  created_at: null,
  frozen_at: null,
};

const counts: ScopeDeletionPlan["counts"] = {
  versions: 2,
  runs: 1,
  papers: 28381,
  papers_kept: 0,
  decided_by_person: 12,
  files: 2627,
  parsed_texts: 2500,
  values: 0,
  value_reviews: 0,
  draft_releases: 0,
  tasks: 3,
};

const plan: ScopeDeletionPlan = {
  scope_id: "s-1",
  name: "Marine toxicology",
  versions: [{ id: "sv-1", version: 2, approval_state: "approved" }],
  counts,
  blockers: [],
  deletable: true,
};

/**
 * Deleting a scope purges tens of thousands of rows, so the request runs for
 * minutes. Without a changing label the press looks like it did nothing, which
 * is what was reported.
 */
describe("Deleting a whole scope", () => {
  beforeEach(() => vi.clearAllMocks());

  it("stays disabled until the scope's name is typed, then deletes", async () => {
    vi.mocked(api.previewScopeDeletion).mockResolvedValue(plan);
    vi.mocked(api.deleteScope).mockResolvedValue({ deleted: counts });
    const onDeleted = vi.fn();
    const user = userEvent.setup();
    render(<ScopeDeletion scope={scope} onDeleted={onDeleted} />);

    const field = await screen.findByRole("textbox");
    expect(screen.getByRole("button", { name: "Delete the scope" })).toBeDisabled();
    await user.type(field, "Marine toxicolog");
    expect(screen.getByRole("button", { name: "Delete the scope" })).toBeDisabled();
    await user.type(field, "y");
    await user.click(screen.getByRole("button", { name: "Delete the scope" }));

    await waitFor(() => expect(api.deleteScope).toHaveBeenCalledWith("sv-1", "Marine toxicology"));
    await waitFor(() => expect(onDeleted).toHaveBeenCalledWith(expect.stringContaining("28,381 papers")));
  });

  it("says it is working while the request is in flight", async () => {
    vi.mocked(api.previewScopeDeletion).mockResolvedValue(plan);
    let finish: (value: { deleted: ScopeDeletionPlan["counts"] }) => void = () => undefined;
    vi.mocked(api.deleteScope).mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const onDeleted = vi.fn();
    const user = userEvent.setup();
    render(<ScopeDeletion scope={scope} onDeleted={onDeleted} />);

    await user.type(await screen.findByRole("textbox"), "Marine toxicology");
    await user.click(screen.getByRole("button", { name: "Delete the scope" }));

    expect(await screen.findByRole("button", { name: "Deleting…" })).toBeDisabled();
    expect(screen.getByText(/takes a few minutes; leave the page open/)).toBeInTheDocument();
    finish({ deleted: counts });
    await waitFor(() => expect(onDeleted).toHaveBeenCalled());
  });

  it("shows why it was refused and offers the press again", async () => {
    vi.mocked(api.previewScopeDeletion).mockResolvedValue(plan);
    vi.mocked(api.deleteScope).mockRejectedValue(new Error("a conversation is pinned to a release"));
    const user = userEvent.setup();
    render(<ScopeDeletion scope={scope} onDeleted={vi.fn()} />);

    await user.type(await screen.findByRole("textbox"), "Marine toxicology");
    await user.click(screen.getByRole("button", { name: "Delete the scope" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("a conversation is pinned to a release");
    expect(await screen.findByRole("button", { name: "Delete the scope" })).toBeInTheDocument();
  });

  it("offers no confirmation at all while something stops it", async () => {
    vi.mocked(api.previewScopeDeletion).mockResolvedValue({
      ...plan,
      blockers: ["2 background tasks for it are still queued or running; cancel or wait"],
      deletable: false,
    });
    render(<ScopeDeletion scope={scope} onDeleted={vi.fn()} />);

    expect(await screen.findByText(/still queued or running/)).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).toBeNull();
  });
});
