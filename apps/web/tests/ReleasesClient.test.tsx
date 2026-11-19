import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ReleasesClient } from "@/components/ReleasesClient";
import { ApiError } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listReleases: vi.fn(),
    listReleaseGates: vi.fn(),
    publishRelease: vi.fn(),
    buildExport: vi.fn(),
  };
});

const api = await import("@/lib/api");

const APPROVED = {
  id: "rel-3",
  name: "corpus",
  semantic_version: "1.2.0",
  status: "approved",
  manifest_sha256: "abcdef0123456789",
  gate_results: { claims_reviewed: true },
  projection_ready_profiles: ["hashing-v1"],
};

const refusal = new ApiError({
  type: "about:blank",
  title: "schema violation",
  status: 422,
  detail: "release cannot be published: a required benchmark has not passed on it",
  error_class: "schema_violation",
  context: { blockers: ["evaluation:rla-retrieval@1.0.0 has not passed"] },
});

describe("ReleasesClient", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listReleases).mockResolvedValue({ items: [APPROVED], aliases: [] } as never);
    vi.mocked(api.listReleaseGates).mockResolvedValue({
      items: [],
      status: [
        { benchmark_id: "rla-retrieval", version: "1.0.0", key: "rla-retrieval@1.0.0", passed: false, run_id: null, run_status: null },
      ],
    });
  });

  it("shows which required benchmarks an approved release lacks", async () => {
    render(<ReleasesClient />);
    expect(await screen.findByText("rla-retrieval@1.0.0=not run")).toBeInTheDocument();
  });

  it("explains a gate refusal and publishes past it with a reason", async () => {
    vi.mocked(api.publishRelease).mockRejectedValueOnce(refusal).mockResolvedValueOnce({
      alias: "current",
      release_id: "rel-3",
      previous_release_id: null,
    });
    const prompt = vi.spyOn(window, "prompt").mockReturnValue("benchmark predates the new strains");
    render(<ReleasesClient />);
    await userEvent.click(await screen.findByRole("button", { name: "Publish" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("rla-retrieval@1.0.0");

    await userEvent.click(screen.getByRole("button", { name: "Publish anyway (admin)" }));
    expect(api.publishRelease).toHaveBeenLastCalledWith(
      "rel-3",
      "current",
      "benchmark predates the new strains",
    );
    expect(await screen.findByText("corpus@1.2.0 is now current.")).toBeInTheDocument();
    prompt.mockRestore();
  });

  it("builds an export and links each file for download", async () => {
    vi.mocked(api.buildExport).mockResolvedValue({
      valid: true,
      problems: [],
      row_counts: {},
      files: ["README.md", "data/long-form.csv", "data/wide-form.xlsx"],
    });
    render(<ReleasesClient />);
    await userEvent.click(await screen.findByRole("button", { name: "Build export" }));
    const all = await screen.findByRole("link", { name: "Download everything (.zip)" });
    expect(all).toHaveAttribute("href", expect.stringMatching(/\/api\/backend\/exports\/rel-3\/download$/));
    expect(screen.getByRole("link", { name: "wide-form.xlsx" })).toHaveAttribute(
      "href",
      expect.stringMatching(/download\?file=data%2Fwide-form\.xlsx$/),
    );
    expect(screen.getByText("Also in the zip: README.md")).toBeInTheDocument();
  });
});
