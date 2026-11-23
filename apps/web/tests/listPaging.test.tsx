import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DatasheetsClient } from "@/components/DatasheetsClient";
import { ReviewClient } from "@/components/review/ReviewClient";
import type { ExtractionTemplate, ReviewItem, WorkingSheet } from "@/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listReviewQueue: vi.fn(),
    correctionRate: vi.fn(async () => ({})),
    listExtractionTemplates: vi.fn(),
    listExtractionRuns: vi.fn(async () => ({ items: [], next_cursor: null, total: 0 })),
    workingSheet: vi.fn(),
  };
});

const api = await import("@/lib/api");

function item(n: number): ReviewItem {
  return {
    claim_id: `c${n}`,
    revision_id: `r${n}`,
    revision_number: 1,
    work_id: "w1",
    predicate: "lipid_titre",
    subject: "YL-7",
    value: { verbatim: `${n} g/L` },
    validation_status: "valid",
    validation_issues: [],
    abstention_reason: "reported",
    review_status: "unreviewed",
    proposal_source: "model",
    evidence: [],
    previous_revisions: [],
    conflicting_revisions: [],
  } as ReviewItem;
}

function sheet(rows: Record<string, string>[], total: number, next: string | null): WorkingSheet {
  return {
    template: "yeast-lipid-datasheet@1.0.0",
    claims: 120,
    review_status: { unreviewed: 120 },
    long_form: [],
    wide: { columns: ["work_id", "experiment_id", "revision_ids", "strain"], rows, total, next_cursor: next },
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("long lists are read a page at a time", () => {
  it("pages the values waiting for review, and starts again when the filter changes", async () => {
    vi.mocked(api.listReviewQueue).mockImplementation(async (_invalid, options = {}) =>
      options.after
        ? { items: [item(26)], next_cursor: null, total: 26 }
        : { items: Array.from({ length: 25 }, (_, i) => item(i + 1)), next_cursor: "c25", total: 26 },
    );
    const user = userEvent.setup();
    render(<ReviewClient />);
    expect(await screen.findByText("1–25 of 26 values to review")).toBeInTheDocument();
    expect(api.listReviewQueue).toHaveBeenCalledWith(true, { after: null, limit: 25 });

    await user.click(screen.getByRole("button", { name: "Next page of values to review" }));
    expect(await screen.findByText("26–26 of 26 values to review")).toBeInTheDocument();
    expect(api.listReviewQueue).toHaveBeenCalledWith(true, { after: "c25", limit: 25 });

    await user.click(screen.getByRole("checkbox"));
    await waitFor(() => expect(api.listReviewQueue).toHaveBeenLastCalledWith(false, { after: null, limit: 25 }));
  });

  it("searches the working sheet on the server, from its first page", async () => {
    const template: ExtractionTemplate = {
      id: "c1",
      template_id: "yeast-lipid-datasheet",
      version: "1.0.0",
      key: "yeast-lipid-datasheet@1.0.0",
      title: "Yeast lipid datasheet",
      scientific_purpose: "Lipid yields per strain.",
      cardinality: "per_experiment",
      targets: [],
      status: "approved",
      active: true,
      checksum: "abc",
      created_at: null,
      definition: {},
    };
    vi.mocked(api.listExtractionTemplates).mockResolvedValue([template]);
    const rows = Array.from({ length: 50 }, (_, i) => ({
      work_id: `w${i}`,
      experiment_id: "",
      revision_ids: "",
      strain: `YL-${i}`,
    }));
    vi.mocked(api.workingSheet).mockImplementation(async (_template, options = {}) =>
      options.q ? sheet([rows[7]!], 1, null) : sheet(rows, 120, "w49|"),
    );
    const user = userEvent.setup();
    render(<DatasheetsClient />);
    expect(await screen.findByText("1–50 of 120 working sheet rows")).toBeInTheDocument();

    await user.type(screen.getByRole("searchbox", { name: "Search working sheet rows" }), "YL-7{Enter}");
    await waitFor(() =>
      expect(api.workingSheet).toHaveBeenLastCalledWith("yeast-lipid-datasheet@1.0.0", {
        after: null,
        limit: 50,
        q: "YL-7",
      }),
    );
    expect(await screen.findByText("1 working sheet rows")).toBeInTheDocument();
  });
});
