import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DatasheetsClient, EXAMPLE_TEMPLATE } from "@/components/DatasheetsClient";
import { ApiError } from "@/lib/api";
import type { ExtractionTemplate } from "@/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listExtractionTemplates: vi.fn(),
    listExtractionRuns: vi.fn(async () => ({ items: [], next_cursor: null, total: 0 })),
    registerExtractionTemplate: vi.fn(),
    approveExtractionTemplate: vi.fn(),
    estimateExtraction: vi.fn(),
    startExtractionRun: vi.fn(),
    cancelExtractionRun: vi.fn(),
    workingSheet: vi.fn(async () => ({
      template: "yeast-lipid-datasheet@1.0.0",
      claims: 2,
      review_status: { unreviewed: 2 },
      long_form: [],
      wide: {
        columns: ["work_id", "experiment_id", "revision_ids", "lipid_yield", "strain"],
        rows: [
          { work_id: "w1abcdef99", experiment_id: "", revision_ids: "r1", lipid_yield: "0.42 g/g", strain: "YL-7" },
        ],
        total: 1,
        next_cursor: null,
      },
    })),
  };
});

const api = await import("@/lib/api");

/** A one-page answer from a paged endpoint. */
const page = <T,>(items: T[]) => ({ items, next_cursor: null, total: items.length });

function template(overrides: Partial<ExtractionTemplate> = {}): ExtractionTemplate {
  return {
    id: "c1",
    template_id: "yeast-lipid-datasheet",
    version: "1.0.0",
    key: "yeast-lipid-datasheet@1.0.0",
    title: "Yeast lipid datasheet",
    scientific_purpose: "Lipid yields per strain.",
    cardinality: "per_experiment",
    targets: [
      { target_id: "strain", predicate: "strain", object_kind: "entity", unit_expectation: null, required: true, deterministic_metadata: false },
      { target_id: "lipid_yield", predicate: "lipid_yield", object_kind: "quantity", unit_expectation: "g/g", required: true, deterministic_metadata: false },
    ],
    status: "approved",
    active: true,
    checksum: "abc",
    created_at: null,
    definition: {},
    ...overrides,
  };
}

describe("DatasheetsClient", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listExtractionTemplates).mockResolvedValue([template()]);
  });

  it("shows the active template and its working sheet", async () => {
    render(<DatasheetsClient />);
    expect(await screen.findByText("strain* · lipid_yield [g/g]*")).toBeInTheDocument();
    expect(await screen.findByText("0.42 g/g")).toBeInTheDocument();
    expect(screen.getByText(/2 proposals · 2 unreviewed/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Wide CSV" })).toHaveAttribute(
      "href",
      expect.stringMatching(/extraction\/rows\.csv\?shape=wide&template=yeast-lipid-datasheet%401\.0\.0$/),
    );
  });

  it("warns when no template is approved", async () => {
    vi.mocked(api.listExtractionTemplates).mockResolvedValue([template({ status: "draft", active: false })]);
    render(<DatasheetsClient />);
    expect(await screen.findByText(/nothing is extracted after parsing/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(api.approveExtractionTemplate).toHaveBeenCalledWith("yeast-lipid-datasheet", "1.0.0");
  });

  it("registers a template from the example", async () => {
    vi.mocked(api.registerExtractionTemplate).mockResolvedValue({ ...template({ status: "draft", active: false }), created: true });
    render(<DatasheetsClient />);
    await userEvent.click(await screen.findByRole("button", { name: "Add template" }));
    await userEvent.click(screen.getByRole("button", { name: "Start from an example" }));
    await userEvent.click(screen.getByRole("button", { name: "Register" }));
    expect(api.registerExtractionTemplate).toHaveBeenCalledWith({ yaml: EXAMPLE_TEMPLATE });
    expect(await screen.findByRole("status")).toHaveTextContent("Registered as a draft");
  });

  it("lists what is wrong with a rejected template", async () => {
    vi.mocked(api.registerExtractionTemplate).mockRejectedValue(
      new ApiError({
        type: "about:blank",
        title: "schema violation",
        status: 422,
        detail: "the template is not valid",
        error_class: "schema_violation",
        context: { problems: ["semantic_version: Field required"] },
      }),
    );
    render(<DatasheetsClient />);
    await userEvent.click(await screen.findByRole("button", { name: "Add template" }));
    await userEvent.type(screen.getByLabelText("Or paste it"), "template_id: x");
    await userEvent.click(screen.getByRole("button", { name: "Register" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("semantic_version: Field required");
  });

  it("estimates, then runs", async () => {
    vi.mocked(api.estimateExtraction).mockResolvedValue({
      template: "yeast-lipid-datasheet@1.0.0",
      renditions: 40,
      already_extracted: 10,
      to_extract: 30,
      sampled: 25,
      input_tokens: 150000,
      output_tokens: 7200,
      cost_usd: 0.56,
      model: "claude-sonnet-4-6",
    });
    vi.mocked(api.startExtractionRun).mockResolvedValue({ run_id: "r1", template: "t", tasks: 30, rerun: false });
    render(<DatasheetsClient />);
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Template" })).toHaveValue("yeast-lipid-datasheet@1.0.0"));
    await userEvent.click(screen.getByRole("button", { name: "Estimate" }));
    expect(await screen.findByText(/≈ \$0\.56 on claude-sonnet-4-6/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(api.startExtractionRun).toHaveBeenCalledWith({ template: "yeast-lipid-datasheet@1.0.0" });
  });

  it("cancels an unfinished run", async () => {
    vi.mocked(api.listExtractionRuns).mockResolvedValue(page([
      { run_id: "run-123456789", template: "yeast-lipid-datasheet@1.0.0", states: { ready: 3, succeeded: 1 }, total: 4, finished: false, created_at: null, errors: [] },
    ]));
    vi.mocked(api.cancelExtractionRun).mockResolvedValue({ run_id: "run-123456789", cancelled_tasks: 3 });
    render(<DatasheetsClient />);
    expect(await screen.findByText("3 ready, 1 succeeded")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(api.cancelExtractionRun).toHaveBeenCalledWith("run-123456789");
  });
});
