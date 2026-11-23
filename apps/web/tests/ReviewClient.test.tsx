import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ReviewClient } from "@/components/review/ReviewClient";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listReviewQueue: vi.fn(),
    correctionRate: vi.fn(async () => ({ organism: 0.25, titre_g_per_l: 0.6 })),
    decideReview: vi.fn(async () => ({
      revision_id: "rev1",
      revision_number: 2,
      review_status: "corrected",
    })),
  };
});

const api = await import("@/lib/api");

/** A one-page answer from a paged endpoint. */
const page = <T,>(items: T[]) => ({ items, next_cursor: null, total: items.length });

const validItem = {
  claim_id: "c1",
  revision_id: "rev1",
  revision_number: 1,
  work_id: "w1",
  predicate: "lipid_yield",
  subject: "YL-A",
  value: { verbatim: "0.42", unit_verbatim: "g/g" },
  validation_status: "valid",
  validation_issues: [],
  abstention_reason: "reported",
  review_status: "unreviewed",
  proposal_source: "model",
  evidence: [
    {
      span_id: "sp1",
      node_id: "p-7",
      quote_preview: "a lipid yield of 0.42 g/g substrate",
      locator: { node_id: "p-7", page: 4 },
      normalisation_version: "1",
    },
  ],
  previous_revisions: [],
  conflicting_revisions: [],
};

const invalidItem = {
  ...validItem,
  claim_id: "c2",
  revision_id: "rev2",
  value: { verbatim: "0.91" },
  validation_status: "invalid",
  validation_issues: ["quote_not_in_node: evidence quote does not match the rendition"],
  evidence: [],
};

describe("ReviewClient", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows the evidence with its locator and normalisation version", async () => {
    vi.mocked(api.listReviewQueue).mockResolvedValue(page([validItem]));
    render(<ReviewClient />);
    expect(await screen.findByText(/a lipid yield of 0.42 g\/g substrate/)).toBeInTheDocument();
    expect(screen.getByText(/p-7 · normalisation v1/)).toBeInTheDocument();
  });

  it("accepts a valid proposal with optimistic concurrency", async () => {
    vi.mocked(api.listReviewQueue).mockResolvedValue(page([validItem]));
    render(<ReviewClient />);
    await userEvent.click(await screen.findByRole("button", { name: /accept/i }));
    await waitFor(() =>
      expect(api.decideReview).toHaveBeenCalledWith("rev1", {
        decision: "accept",
        reason_code: "evidence_supports_value",
        expected_revision_number: 1,
        corrected_value: undefined,
      }),
    );
  });

  it("refuses to offer Accept for a proposal that failed validation", async () => {
    vi.mocked(api.listReviewQueue).mockResolvedValue(page([invalidItem]));
    render(<ReviewClient />);
    expect(await screen.findByText(/quote_not_in_node/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /accept/i })).toBeDisabled();
    expect(screen.getByText(/No verified evidence span/)).toBeInTheDocument();
  });

  it("sends a correction as a new value", async () => {
    vi.mocked(api.listReviewQueue).mockResolvedValue(page([validItem]));
    render(<ReviewClient />);
    await userEvent.type(await screen.findByLabelText(/corrected value/i), "0.43");
    await userEvent.click(screen.getByRole("button", { name: /^correct$/i }));
    await waitFor(() =>
      expect(api.decideReview).toHaveBeenCalledWith(
        "rev1",
        expect.objectContaining({
          decision: "correct",
          corrected_value: expect.objectContaining({ verbatim: "0.43" }),
        }),
      ),
    );
  });

  it("reports a concurrency conflict rather than silently overwriting", async () => {
    vi.mocked(api.listReviewQueue).mockResolvedValue(page([validItem]));
    vi.mocked(api.decideReview).mockRejectedValue(new Error("this claim changed since it was read"));
    render(<ReviewClient />);
    await userEvent.click(await screen.findByRole("button", { name: /accept/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/changed since it was read/);
  });
});

describe("CorrectionRates", () => {
  it("lists predicates, most corrected first", async () => {
    vi.mocked(api.listReviewQueue).mockResolvedValue(page([]));
    render(<ReviewClient />);
    const table = await screen.findByRole("table");
    const cells = within(table).getAllByRole("cell").map((cell) => cell.textContent);
    expect(cells).toEqual(["titre_g_per_l", "60.0%", "organism", "25.0%"]);
  });
});
