import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AnswerReviewsClient } from "@/components/AnswerReviewsClient";
import type { AnswerReview } from "@/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, listMyAnswerReviews: vi.fn(), submitAnswerReview: vi.fn() };
});

const api = await import("@/lib/api");

const ASSIGNED: AnswerReview = {
  id: "rv1",
  run_id: "run-g",
  case_id: "lipid-yield",
  assigned_to: "u1",
  assigned_by: "u2",
  status: "assigned",
  scores: {},
  flags: [],
  reviewer_confidence: null,
  comment: null,
  suggested_answer: null,
  created_at: null,
  submitted_at: null,
  case: {
    question: "What lipid yield was reported?",
    answer: "About 40% of dry cell weight [E0123456789].",
    citation_validity: 1,
    unsupported_numbers: [],
  },
};

const SCORES = ["correctness", "completeness", "citation_support", "grounding", "usefulness"];
const FLAGS = ["hallucination", "citation_issue", "corpus_gap", "retrieval_issue", "generation_issue"];

describe("AnswerReviewsClient", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listMyAnswerReviews).mockResolvedValue({ items: [ASSIGNED], next_cursor: null, total: 1, to_do: 1, scores: SCORES, flags: FLAGS });
  });

  it("shows the question, the answer and what was measured", async () => {
    render(<AnswerReviewsClient />);
    expect(await screen.findByText("What lipid yield was reported?")).toBeInTheDocument();
    expect(screen.getByText(/About 40% of dry cell weight/)).toBeInTheDocument();
    expect(screen.getByText(/citation validity 1/)).toBeInTheDocument();
    expect(screen.getByText("1 waiting, 0 graded.")).toBeInTheDocument();
  });

  it("needs correctness before it can be submitted, then submits the form", async () => {
    vi.mocked(api.submitAnswerReview).mockResolvedValue({ ...ASSIGNED, status: "submitted" });
    render(<AnswerReviewsClient />);
    const submit = await screen.findByRole("button", { name: "Submit review" });
    expect(submit).toBeDisabled();

    await userEvent.selectOptions(screen.getByRole("combobox", { name: "correctness" }), "4");
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "grounding" }), "5");
    await userEvent.click(screen.getByRole("checkbox", { name: "corpus gap" }));
    await userEvent.type(screen.getByLabelText("Comment"), "Right, but misses the 2024 strain.");
    await userEvent.click(submit);

    expect(api.submitAnswerReview).toHaveBeenCalledWith("rv1", {
      scores: { correctness: 4, grounding: 5 },
      flags: ["corpus_gap"],
      reviewer_confidence: null,
      comment: "Right, but misses the 2024 strain.",
      suggested_answer: null,
    });
    expect(await screen.findByRole("status")).toHaveTextContent("Saved.");
  });

  it("says when nothing is waiting", async () => {
    vi.mocked(api.listMyAnswerReviews).mockResolvedValue({ items: [], next_cursor: null, total: 0, to_do: 0, scores: SCORES, flags: FLAGS });
    render(<AnswerReviewsClient />);
    expect(await screen.findByText("No answer is waiting for your review.")).toBeInTheDocument();
  });
});
