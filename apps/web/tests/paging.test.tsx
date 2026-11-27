import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CollectionsClient } from "@/components/CollectionsClient";
import type { Candidate } from "@/types";

const QUALITY = {
  limits: { max_duplicate_paragraph_ratio: null, require_reference_separation: null },
  defaults: {
    "application/pdf": { profile: "pdf-born-digital", max_duplicate_paragraph_ratio: 0.1, require_reference_separation: false },
    "application/xml": { profile: "jats-default", max_duplicate_paragraph_ratio: 0.02, require_reference_separation: true },
  },
  set_at: null,
  degraded: 341,
  degraded_by_reason: { "reference list not separated": 200, "too many repeated paragraphs": 141 },
};

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listScopes: vi.fn(async () => []),
    listDiscoveryRuns: vi.fn(async () => ({ items: [], next_cursor: null, total: 0 })),
    listCandidates: vi.fn(),
    fetchIncluded: vi.fn(),
    fetchSweep: vi.fn(),
    getParseQuality: vi.fn(async () => QUALITY),
    getAbstractFallback: vi.fn(async () => ({
      enabled: false,
      set_at: null,
      set_by: null,
      partial: 0,
      eligible: 0,
    })),
    setParseLimits: vi.fn(async () => ({ ...QUALITY, limits: { max_duplicate_paragraph_ratio: 0.2, require_reference_separation: false } })),
    listPlans: vi.fn(async () => ({ items: [], next_cursor: null, total: 0, status_counts: {} })),
    listConflicts: vi.fn(async () => ({
      conflict_total: 0,
      proposal_total: 0,
      next_cursor: null,
      duplicate_proposals: [],
    })),
    candidateFacets: vi.fn(async () => ({
      languages: { en: 40, es: 6, unknown: 5 },
      kinds: { article: 51 },
      sources: { pubmed: 51 },
      year_min: 2000,
      year_max: 2026,
      summarised: 51,
      works: 51,
    })),
  };
});

const api = await import("@/lib/api");

function candidate(n: number): Candidate {
  return {
    work_id: `work-${n}`,
    title: `Paper ${n}`,
    year: 2024,
    journal: null,
    work_kind: "article",
    identity_status: "resolved",
    relevance: "unreviewed",
    retraction_status: "none",
    identifiers: [],
    provenance: {},
  };
}

describe("The candidate list is read a page at a time", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listCandidates).mockImplementation(async (options = {}) =>
      options.after
        ? { items: [candidate(51)], next_cursor: null, total: 51 }
        : {
            items: Array.from({ length: 50 }, (_, i) => candidate(i + 1)),
            next_cursor: "work-50",
            total: 51,
          },
    );
  });

  it("shows one page and the total, and moves between pages", async () => {
    const user = userEvent.setup();
    render(<CollectionsClient />);
    expect(await screen.findAllByText("1–50 of 51 candidates")).toHaveLength(2); // at the top and under the list
    expect(screen.queryByText("Paper 51")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Next page of candidates" }));
    expect(await screen.findByText("Paper 51")).toBeInTheDocument();
    expect(screen.getAllByText("51–51 of 51 candidates")).toHaveLength(2);
    expect(screen.queryByText("Paper 1")).toBeNull();
    expect(api.listCandidates).toHaveBeenLastCalledWith(
      expect.objectContaining({ after: "work-50", limit: 50 }),
    );

    await user.click(screen.getByRole("button", { name: "Previous page of candidates" }));
    expect(await screen.findByText("Paper 1")).toBeInTheDocument();
  });

  it("filters on the server and starts again from the first page", async () => {
    const user = userEvent.setup();
    render(<CollectionsClient />);
    await screen.findAllByText("1–50 of 51 candidates");
    await user.click(screen.getByRole("button", { name: "Next page of candidates" }));
    await screen.findByText("Paper 51");

    await user.selectOptions(screen.getByRole("combobox", { name: "Filter by relevance" }), "included");
    await waitFor(() =>
      expect(api.listCandidates).toHaveBeenLastCalledWith(
        expect.objectContaining({ relevance: "included", after: null }),
      ),
    );
  });
});

describe("Fetching included papers", () => {
  it("queues the sweep and says it carries on without the page", async () => {
    vi.mocked(api.listCandidates).mockResolvedValue({ items: [], next_cursor: null, total: 0 });
    vi.mocked(api.fetchIncluded).mockResolvedValue({ task_id: "t1", state: "ready" });
    // Left pending: the press returns as soon as the task is queued, and the
    // watching is the subject of its own tests below.
    vi.mocked(api.fetchSweep).mockReturnValue(new Promise(() => {}));
    const user = userEvent.setup();
    render(<CollectionsClient />);

    await user.click(await screen.findByRole("button", { name: "Fetch included papers" }));

    expect(api.fetchIncluded).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/Queued\./)).toBeInTheDocument();
    expect(screen.getByText(/carries on whether or not you stay on this page/)).toBeInTheDocument();
  });
});

describe("Long lists on the Collections page", () => {
  it("say how many rows they hold at the top, set rows per page, and fold away", async () => {
    vi.mocked(api.listCandidates).mockImplementation(async (options = {}) => ({
      items: Array.from({ length: Math.min(options.limit ?? 50, 51) }, (_, i) => candidate(i + 1)),
      next_cursor: (options.limit ?? 50) < 51 ? "more" : null,
      total: 51,
    }));
    const user = userEvent.setup();
    render(<CollectionsClient />);
    expect(await screen.findAllByText("1–50 of 51 candidates")).toHaveLength(2);

    await user.selectOptions(screen.getByRole("combobox", { name: "Rows per page of candidates" }), "10");
    await waitFor(() => expect(api.listCandidates).toHaveBeenLastCalledWith(expect.objectContaining({ limit: 10 })));
    expect(await screen.findAllByText("1–10 of 51 candidates")).toHaveLength(2);

    const hide = screen.getByRole("button", { name: "Hide Candidates" });
    expect(hide).toHaveAttribute("aria-expanded", "true");
    await user.click(hide);
    expect(screen.getByRole("button", { name: "Show Candidates" })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("combobox", { name: "Rows per page of candidates" })).toBeNull();
    expect(screen.getByRole("button", { name: "Hide Acquisition queue" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Hide Duplicates and identity conflicts" })).toBeInTheDocument();
  });
});


describe("Parse quality on the Collections page", () => {
  it("shows the two limits with each format's default, and saves new ones", async () => {
    vi.mocked(api.listCandidates).mockResolvedValue({ items: [], next_cursor: null, total: 0 });
    const user = userEvent.setup();
    render(<CollectionsClient />);
    expect(await screen.findByText("341 degraded documents")).toBeInTheDocument();
    expect(screen.getByText(/200 — reference list not separated/)).toBeInTheDocument();
    const share = screen.getByLabelText("Repeated paragraphs allowed, up to (%)");
    expect(share).toHaveAttribute("placeholder", "PDF 10%, XML (JATS) 2%");

    await user.type(share, "20");
    await user.selectOptions(
      screen.getByLabelText("Reference list must be separated from the text"),
      "no",
    );
    await user.click(screen.getByRole("button", { name: "Save limits" }));
    expect(api.setParseLimits).toHaveBeenCalledWith({
      max_duplicate_paragraph_ratio: 0.2,
      require_reference_separation: false,
    });
    expect(await screen.findByText(/checked against them when you next press Fetch included papers/)).toBeInTheDocument();
  });
});

describe("Uploading an authorised copy", () => {
  const plan = {
    plan_id: "plan-1",
    work_id: "work-1",
    paper: { title: "Lipids in Yarrowia", year: 2024, journal: null, first_author: null },
    identifiers: [],
    file: null,
    status: "assisted",
    routes: ["user_assisted"],
    manifestation_ids: ["man-1"],
    attempts: [],
  };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listCandidates).mockResolvedValue({ items: [], next_cursor: null, total: 0 });
    vi.mocked(api.listPlans).mockResolvedValue({
      items: [plan] as never,
      next_cursor: null,
      total: 1,
      status_counts: { assisted: 1 },
    });
  });

  it("opens the form in the paper's own row, says why, and reports the result", async () => {
    vi.spyOn(api, "assistedUpload").mockResolvedValue({
      asset_id: "asset-1",
      identity_verified: true,
      malware_scan_status: "passed",
    });
    const user = userEvent.setup();
    render(<CollectionsClient />);
    await user.click(await screen.findByRole("button", { name: "Upload an authorised copy" }));

    const form = screen.getByRole("region", { name: "Upload an authorised copy" });
    // Inside the plan's row, not at the foot of the page.
    expect(form.closest("li")).toHaveTextContent("Lipids in Yarrowia");
    expect(form).toHaveTextContent(/only an upload can bring it in/);

    await user.upload(
      within(form).getByLabelText("File"),
      new File(["%PDF-1.7"], "paper.pdf", { type: "application/pdf" }),
    );
    await user.type(within(form).getByPlaceholderText("How was this copy obtained?"), "Library copy");
    await user.click(within(form).getByRole("button", { name: "Upload" }));

    expect(api.assistedUpload).toHaveBeenCalledWith(
      "plan-1",
      expect.objectContaining({ manifestationId: "man-1", attestation: "Library copy" }),
    );
    expect(await screen.findByText(/Uploaded for “Lipids in Yarrowia”: identity confirmed/)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Upload an authorised copy" })).not.toBeInTheDocument();
  });

  it("closes again from the same button", async () => {
    const user = userEvent.setup();
    render(<CollectionsClient />);
    await user.click(await screen.findByRole("button", { name: "Upload an authorised copy" }));
    await user.click(screen.getByRole("button", { name: "Close the upload" }));
    expect(screen.queryByRole("region", { name: "Upload an authorised copy" })).not.toBeInTheDocument();
  });
});


describe("Filtering candidates by language and by the abstract", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // An earlier test folds the panel away, and the panel remembers.
    window.localStorage.clear();
    vi.mocked(api.listCandidates).mockResolvedValue({ items: [], next_cursor: null, total: 0 });
    vi.mocked(api.candidateFacets).mockResolvedValue({
      languages: { en: 40, es: 6, unknown: 5 },
      kinds: { article: 51 },
      sources: { pubmed: 51 },
      year_min: 2000,
      year_max: 2026,
      summarised: 51,
      works: 51,
    });
  });

  it("finds papers not in English, and searches the abstract when asked", async () => {
    const user = userEvent.setup();
    render(<CollectionsClient />);
    const menu = await screen.findByLabelText("Filter by language");
    expect(
      await screen.findByRole("option", { name: "Spanish (6)" }, { timeout: 4000 }),
    ).toBeInTheDocument();

    await user.selectOptions(menu, "not:en,unknown");
    await waitFor(() =>
      expect(api.listCandidates).toHaveBeenLastCalledWith(
        expect.objectContaining({ exclude_language: "en,unknown", language: undefined }),
      ),
    );
    await user.selectOptions(menu, "is:es");
    await waitFor(() =>
      expect(api.listCandidates).toHaveBeenLastCalledWith(
        expect.objectContaining({ language: "es", exclude_language: undefined }),
      ),
    );
    await user.click(screen.getByLabelText(/in the abstract and subject terms too/));
    await waitFor(() =>
      expect(api.listCandidates).toHaveBeenLastCalledWith(expect.objectContaining({ in_abstract: true })),
    );
  });
});


describe("The Collections page groups its settings and searches its queue", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    vi.mocked(api.listCandidates).mockResolvedValue({ items: [], next_cursor: null, total: 0 });
    vi.mocked(api.listPlans).mockResolvedValue({ items: [], next_cursor: null, total: 0, status_counts: {} });
  });

  it("holds parse quality and the abstracts in one Collection settings box", async () => {
    render(<CollectionsClient />);
    expect(await screen.findByRole("heading", { name: "Collection settings" })).toBeInTheDocument();
    const settings = screen.getByRole("heading", { name: "Collection settings" }).closest("section")!;
    expect(within(settings).getByRole("region", { name: "Parse quality" })).toBeInTheDocument();
    expect(within(settings).getByRole("region", { name: "Abstracts for papers without full text" })).toBeInTheDocument();
  });

  it("searches the acquisition queue by title or identifier and by the last answer", async () => {
    const user = userEvent.setup();
    render(<CollectionsClient />);
    await user.type(await screen.findByLabelText("Title or identifier has"), "10.1038");
    const queue = screen.getByLabelText("Title or identifier has").closest("form")!;
    await user.click(within(queue).getByRole("button", { name: "Search" }));
    await waitFor(() =>
      expect(api.listPlans).toHaveBeenLastCalledWith(expect.objectContaining({ q: "10.1038" })),
    );
    await user.selectOptions(screen.getByLabelText("Filter plans by the last answer"), "none");
    await waitFor(() =>
      expect(api.listPlans).toHaveBeenLastCalledWith(expect.objectContaining({ q: "10.1038", outcome: "none" })),
    );
  });
});

describe("The acquisition queue downloads what its search finds", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    vi.mocked(api.listCandidates).mockResolvedValue({ items: [], next_cursor: null, total: 0 });
    vi.mocked(api.listPlans).mockResolvedValue({
      items: [],
      next_cursor: null,
      total: 1234,
      status_counts: { assisted: 1234 },
    });
  });

  it("links one CSV and a zip of CSVs, for every page of the search as applied", async () => {
    const user = userEvent.setup();
    render(<CollectionsClient />);
    const csv = await screen.findByRole("link", { name: "One CSV" });
    const zip = screen.getByRole("link", { name: "A zip of CSVs, 100 papers each" });
    expect(screen.getByText("Download all 1,234 plans, on every page:")).toBeInTheDocument();
    expect(csv).toHaveAttribute("href", "/assist/api/backend/acquisition-plans/export.csv");
    expect(zip).toHaveAttribute("href", "/assist/api/backend/acquisition-plans/export.zip");
    expect(csv).toHaveAttribute("download");

    // Words still being typed are not the search yet.
    await user.type(screen.getByLabelText("Title or identifier has"), "sterol");
    expect(csv).toHaveAttribute("href", "/assist/api/backend/acquisition-plans/export.csv");
    const queue = screen.getByLabelText("Title or identifier has").closest("form")!;
    await user.click(within(queue).getByRole("button", { name: "Search" }));
    await user.selectOptions(screen.getByLabelText("Filter by status"), "assisted");
    await waitFor(() =>
      expect(screen.getByRole("link", { name: "One CSV" })).toHaveAttribute(
        "href",
        "/assist/api/backend/acquisition-plans/export.csv?status=assisted&q=sterol",
      ),
    );
    expect(screen.getByRole("link", { name: "A zip of CSVs, 100 papers each" })).toHaveAttribute(
      "href",
      "/assist/api/backend/acquisition-plans/export.zip?status=assisted&q=sterol",
    );
    expect(screen.getByText("Download the 1,234 plans this search finds, on every page:")).toBeInTheDocument();
  });

  it("offers nothing when the search finds nothing", async () => {
    vi.mocked(api.listPlans).mockResolvedValue({ items: [], next_cursor: null, total: 0, status_counts: {} });
    render(<CollectionsClient />);
    expect(await screen.findByText("Nothing to download: no plan matches this search.")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "One CSV" })).not.toBeInTheDocument();
  });
});
