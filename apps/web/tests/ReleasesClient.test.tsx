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
    draftRelease: vi.fn(),
    releaseChanges: vi.fn(),
    reindexRelease: vi.fn(),
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
    vi.mocked(api.listReleases).mockResolvedValue({ items: [APPROVED], aliases: [], next_cursor: null, total: 1 } as never);
    vi.mocked(api.listReleaseGates).mockResolvedValue({
      items: [],
      status: [
        { benchmark_id: "rla-retrieval", version: "1.0.0", key: "rla-retrieval@1.0.0", passed: false, run_id: null, run_status: null },
      ],
    });
  });

  it("offers to delete an approved release only while it was never published", async () => {
    vi.mocked(api.listReleases).mockResolvedValue({
      items: [
        { ...APPROVED, published_at: null },
        { ...APPROVED, id: "rel-4", semantic_version: "1.3.0", status: "published", published_at: "2026-11-20T10:00:00Z" },
        { ...APPROVED, id: "rel-5", semantic_version: "1.4.0", status: "validated", published_at: null },
      ],
      aliases: [],
      next_cursor: null,
      total: 3,
    } as never);
    render(<ReleasesClient />);
    expect(await screen.findByRole("button", { name: /Delete release corpus@1\.2\.0/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Delete release corpus@1\.4\.0/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Delete release corpus@1\.3\.0/ })).toBeNull();
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

  it("drafts after the current release and says what changed since it", async () => {
    const PUBLISHED = { ...APPROVED, id: "rel-2", semantic_version: "1.1.0", status: "published", published_at: "2026-11-01T10:00:00Z" };
    vi.mocked(api.listReleases).mockResolvedValue({
      items: [PUBLISHED],
      aliases: [{ alias: "current", release_id: "rel-2", previous_release_id: null }],
      next_cursor: null,
      total: 1,
    } as never);
    vi.mocked(api.draftRelease).mockResolvedValue({
      release_id: "rel-9",
      status: "draft",
      manifest_sha256: "f",
      items: 1230,
      diff: { added: 12, changed: 3, removed: 1, unchanged: 1215, full_build: false },
    });
    render(<ReleasesClient />);
    await userEvent.click(await screen.findByRole("button", { name: "Draft from approved material" }));
    expect(api.draftRelease).toHaveBeenCalledWith(expect.objectContaining({ parent_release_id: "rel-2" }));
    expect(
      await screen.findByText(/with 1,230 papers\. Since corpus@1\.1\.0: 12 added, 3 changed, 1 removed, 1,215 unchanged\./),
    ).toBeInTheDocument();
  });

  it("drafts a first release with nothing to follow", async () => {
    vi.mocked(api.draftRelease).mockResolvedValue({
      release_id: "rel-9",
      status: "draft",
      manifest_sha256: "f",
      items: 1,
      diff: { added: 1, changed: 0, removed: 0, unchanged: 0, full_build: true },
    });
    render(<ReleasesClient />);
    await userEvent.click(await screen.findByRole("button", { name: "Draft from approved material" }));
    expect(vi.mocked(api.draftRelease).mock.calls[0]?.[0]).not.toHaveProperty("parent_release_id");
    expect(await screen.findByText(/with 1 paper\. Nothing is published yet/)).toBeInTheDocument();
  });

  it("shows what a release adds and drops, and why, when opened", async () => {
    vi.mocked(api.listReleases).mockResolvedValue({
      items: [
        { ...APPROVED, id: "rel-9", semantic_version: "1.3.0", status: "draft", parent_release_id: "rel-3" },
        { ...APPROVED, parent_release_id: null },
      ],
      aliases: [],
      next_cursor: null,
      total: 2,
    } as never);
    vi.mocked(api.releaseChanges).mockResolvedValue({
      release_id: "rel-9",
      parent: { id: "rel-3", name: "corpus", semantic_version: "1.2.0" },
      items: 11,
      added: 3,
      changed: 0,
      removed: 1,
      unchanged: 8,
      papers: {
        added: [{ work_id: "w-1", title: "Lipid bodies in Yarrowia", year: 2024, dois: ["10.1/x"] }],
        changed: [],
        removed: [{ work_id: "w-2", title: "A duplicate", year: 2023, dois: [], why: "merged" }],
      },
    });
    render(<ReleasesClient />);
    // Only the release that follows another offers the comparison.
    const summaries = await screen.findAllByText(/^What changed since/);
    expect(summaries).toHaveLength(1);
    expect(api.releaseChanges).not.toHaveBeenCalled();
    await userEvent.click(screen.getByText("What changed since corpus@1.2.0"));
    expect(await screen.findByText(/11 papers: 3 added, 0 changed, 1 removed, 8 unchanged\./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "10.1/x" })).toHaveAttribute("href", "https://doi.org/10.1/x");
    expect(screen.getByText(/merged into another paper/)).toBeInTheDocument();
    expect(screen.getByText(/and 2 more not listed here/)).toBeInTheDocument();
    expect(screen.queryByText(/^Changed/)).toBeNull();
  });

  it("offers a rebuild only when an index failed its chunk-count check", async () => {
    vi.mocked(api.listReleases).mockResolvedValue({
      items: [
        { ...APPROVED, gate_results: { claims_reviewed: true, projection_membership: false } },
        { ...APPROVED, id: "rel-4", semantic_version: "1.3.0" },
      ],
      aliases: [],
      next_cursor: null,
      total: 2,
    } as never);
    vi.mocked(api.reindexRelease).mockResolvedValue({
      release_id: "rel-3",
      status: "approved",
      indexing_tasks: ["task-1"],
    });
    render(<ReleasesClient />);
    const rebuild = await screen.findAllByRole("button", { name: "Rebuild index" });
    expect(rebuild).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Publish" })[0]).toBeDisabled();
    await userEvent.click(rebuild[0]!);
    expect(api.reindexRelease).toHaveBeenCalledWith("rel-3");
    expect(
      await screen.findByText(/Rebuilding the index of corpus@1\.2\.0\. Publish is offered once it passes its check\./),
    ).toBeInTheDocument();
  });
});
