import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { QueryHelper } from "@/components/QueryHelper";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, lookupOrganism: vi.fn(), lookupProduct: vi.fn(), buildSourceQueries: vi.fn() };
});

const api = await import("@/lib/api");

describe("QueryHelper", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.lookupOrganism).mockImplementation(async (params) =>
      params.taxid
        ? {
            suggestions: [],
            seed: {
              taxid: 4952,
              scientific_name: "Yarrowia lipolytica",
              rank: "species",
              synonyms: ["Candida lipolytica"],
              common_names: [],
              lineage: [],
              search_terms: ["Yarrowia lipolytica", "Candida lipolytica"],
            },
          }
        : {
            suggestions: [{ taxid: 4952, scientific_name: "Yarrowia lipolytica", rank: "species", common_name: null }],
            seed: null,
          },
    );
    vi.mocked(api.buildSourceQueries).mockResolvedValue({
      pubmed: '("Yarrowia lipolytica"[Title/Abstract])',
      crossref: "Yarrowia lipolytica",
    });
  });

  it("looks up an organism, keeps chosen synonyms and writes the queries", async () => {
    const onQueries = vi.fn();
    render(<QueryHelper onQueries={onQueries} />);
    await userEvent.type(screen.getByRole("textbox", { name: "Organism" }), "Yarrowia lipo");
    await userEvent.click(await screen.findByRole("button", { name: "Yarrowia lipolytica" }));
    expect(api.lookupOrganism).toHaveBeenLastCalledWith({ taxid: 4952 });

    // Every name starts chosen; a name the literature does not use can be dropped.
    const synonym = await screen.findByRole("checkbox", { name: "Candida lipolytica" });
    expect(synonym).toBeChecked();
    await userEvent.click(synonym);
    await userEvent.click(screen.getByRole("button", { name: "Write the source queries (1 names)" }));
    expect(api.buildSourceQueries).toHaveBeenCalledWith(["Yarrowia lipolytica"], []);
    await waitFor(() =>
      expect(onQueries).toHaveBeenCalledWith({
        pubmed: '("Yarrowia lipolytica"[Title/Abstract])',
        crossref: "Yarrowia lipolytica",
      }),
    );
    // The chosen name is not looked up again.
    expect(screen.queryByRole("list", { name: "Organism suggestions" })).not.toBeInTheDocument();
  });

  it("says when the upstream fails, and leaves writing by hand open", async () => {
    vi.mocked(api.lookupOrganism).mockRejectedValue(new Error("eutils.ncbi.nlm.nih.gov answered 503; try again shortly"));
    render(<QueryHelper onQueries={vi.fn()} />);
    await userEvent.type(screen.getByRole("textbox", { name: "Organism" }), "Yarrowia");
    expect(await screen.findByRole("status")).toHaveTextContent("write the queries by hand");
  });
});
