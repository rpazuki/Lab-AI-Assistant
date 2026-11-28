import { describe, expect, it } from "vitest";

import { answerAsMarkdown, exportFileName, numberCitations, reference } from "@/lib/answerExport";
import type { SourceBlock } from "@/types";

function source(handle: string, extra: Partial<SourceBlock> = {}): SourceBlock {
  return {
    handle,
    title: `Paper ${handle}`,
    year: 2024,
    source_label: null,
    locator: null,
    display_restricted: false,
    snippet: "",
    ...extra,
  };
}

const A = source("Eaaaaaaaaaa", {
  title: "Lipid production in Yarrowia",
  authors: ["Pazuki R", "Ledesma-Amaro R"],
  journal: "Metab Eng",
  doi: "10.1/x",
  pmid: "123",
});
const B = source("Ebbbbbbbbbb");
const C = source("Ecccccccccc");

describe("exporting an answer", () => {
  it("numbers citations by first use, so the reference list has no gaps", () => {
    const { text, cited } = numberCitations(
      "B first [Ebbbbbbbbbb], then A [Eaaaaaaaaaa], B again [Ebbbbbbbbbb, Eaaaaaaaaaa].",
      [A, B, C],
    );
    expect(text).toBe("B first [1], then A [2], B again [1, 2].");
    expect(cited.map((s) => s.handle)).toEqual([B.handle, A.handle]);
  });

  it("writes a Vancouver-style reference", () => {
    expect(reference(A)).toBe(
      "Pazuki R, Ledesma-Amaro R. Lipid production in Yarrowia. Metab Eng. 2024. doi:10.1/x. PMID: 123.",
    );
    const many = source("Edddddddddd", {
      authors: ["A A", "B B", "C C", "D D", "E E", "F F", "G G"],
      abstract_only: true,
    });
    expect(reference(many)).toMatch(/^A A, B B, C C, D D, E E, F F, et al\. /);
    expect(reference(many)).toMatch(/\(Abstract only\.\)$/);
  });

  it("puts the question, the answer, the references and the release together", () => {
    const markdown = answerAsMarkdown(
      {
        question: "What yield?",
        answer: "0.42 g/g [Eaaaaaaaaaa], and something unsupported [Edeadbeef01].",
        sources: [A, B],
        releaseLabel: "rla-corpus 1.1.0",
        unsupported: ["Edeadbeef01"],
      },
      new Date("2026-11-28T10:00:00Z"),
    );
    expect(markdown).toContain("## What yield?");
    expect(markdown).toContain("0.42 g/g [1], and something unsupported [?].");
    expect(markdown).toContain("Citations marked [?]");
    expect(markdown).toContain("### References\n\n1. Pazuki R, Ledesma-Amaro R.");
    expect(markdown).not.toContain("Paper Ebbbbbbbbbb");
    expect(markdown).toContain("_From the lab corpus, release rla-corpus 1.1.0, 2026-11-28._");
  });

  it("names the file after the question", () => {
    expect(exportFileName("What lipid yield was reported?")).toBe("what-lipid-yield-was-reported.md");
    expect(exportFileName("???")).toBe("answer.md");
  });
});
