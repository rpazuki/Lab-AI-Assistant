import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { BulkImport } from "@/components/BulkImport";
import type { AcquisitionPlan } from "@/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, bulkImport: vi.fn(), assistedUpload: vi.fn(async () => ({})) };
});

const api = await import("@/lib/api");

const WAITING = {
  plan_id: "plan-0002-aaaa",
  work_id: "work-0002-bbbb",
  status: "assisted",
  routes: ["user_assisted"],
  manifestation_ids: ["man-2"],
  attempts: [],
} as unknown as AcquisitionPlan;

const pdf = (name: string) => new File(["%PDF-1.7"], name, { type: "application/pdf" });

describe("BulkImport", () => {
  beforeEach(() => vi.clearAllMocks());

  it("checks matches without storing anything", async () => {
    vi.mocked(api.bulkImport).mockResolvedValue({
      dry_run: true,
      counts: { matched: 1, unmatched: 1 },
      items: [
        { filename: "10.1234_a.pdf", outcome: "matched", identifiers: ["doi:10.1234/a"], plan_id: "plan-1", work_id: "w1", asset_id: null, parse_task_id: null, detail: null },
        { filename: "notes.pdf", outcome: "unmatched", identifiers: [], plan_id: null, work_id: null, asset_id: null, parse_task_id: null, detail: "no DOI, PMID or PMCID in the file name or first page" },
      ],
    });
    const onImported = vi.fn();
    render(<BulkImport waiting={[WAITING]} onImported={onImported} />);
    await userEvent.upload(screen.getByLabelText("PDF files"), [pdf("10.1234_a.pdf"), pdf("notes.pdf")]);
    expect(screen.getByText("2 PDFs chosen")).toBeInTheDocument();
    // Importing needs an attestation; checking does not.
    expect(screen.getByRole("button", { name: "Import" })).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: "Check matches" }));
    expect(vi.mocked(api.bulkImport).mock.calls[0]?.[1]).toMatchObject({ dryRun: true });
    expect(await screen.findByRole("status")).toHaveTextContent("Nothing was stored. 1 matched, 1 unmatched.");
    expect(onImported).not.toHaveBeenCalled();
    // A dry run offers no manual assignment: nothing has been imported yet.
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("imports, then lets an unmatched file be assigned by hand", async () => {
    vi.mocked(api.bulkImport).mockResolvedValue({
      dry_run: false,
      counts: { unmatched: 1 },
      items: [
        { filename: "scan.pdf", outcome: "unmatched", identifiers: [], plan_id: null, work_id: null, asset_id: null, parse_task_id: null, detail: "no DOI, PMID or PMCID in the file name or first page" },
      ],
    });
    const onImported = vi.fn();
    render(<BulkImport waiting={[WAITING]} onImported={onImported} />);
    await userEvent.upload(screen.getByLabelText("PDF files"), [pdf("scan.pdf")]);
    await userEvent.type(screen.getByLabelText(/Attestation/), "Library copy");
    await userEvent.click(screen.getByRole("button", { name: "Import" }));
    expect(onImported).toHaveBeenCalledOnce();

    await userEvent.selectOptions(
      await screen.findByRole("combobox", { name: "Assign scan.pdf" }),
      "plan-0002-aaaa",
    );
    expect(api.assistedUpload).toHaveBeenCalledWith(
      "plan-0002-aaaa",
      expect.objectContaining({ manifestationId: "man-2", attestation: "Library copy" }),
    );
    expect(await screen.findByText("assigned by hand")).toBeInTheDocument();
  });
});
