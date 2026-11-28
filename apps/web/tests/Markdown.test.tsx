import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { Markdown } from "@/components/ask/Markdown";
import { authorLine, markPassage, passageOf, placeLines, pmcUrl, whereLine } from "@/lib/sources";
import type { SourceBlock } from "@/types";

function md(text: string, renderCitation?: (handle: string, key: string) => ReactNode) {
  return render(<Markdown text={text} renderCitation={renderCitation} />).container;
}

describe("Markdown", () => {
  it("renders headings, paragraphs and line breaks", () => {
    const root = md("# Findings\n\nFirst line\nsecond line\n\nNext paragraph.");
    expect(screen.getByRole("heading", { name: "Findings" })).toBeInTheDocument();
    const paragraphs = root.querySelectorAll("p");
    expect(paragraphs).toHaveLength(2);
    expect(paragraphs[0]!.querySelector("br")).not.toBeNull();
  });

  it("renders bold, italics, code and nested lists", () => {
    const root = md(
      "Some **bold**, *italic* and `code`.\n\n1. First\n   - inner a\n   - inner b\n2. Second",
    );
    expect(screen.getByText("bold").tagName).toBe("STRONG");
    expect(screen.getByText("italic").tagName).toBe("EM");
    expect(screen.getByText("code").tagName).toBe("CODE");
    const ordered = root.querySelector("ol")!;
    expect(ordered.children).toHaveLength(2);
    expect(ordered.querySelector("ul")!.children).toHaveLength(2);
  });

  it("keeps a numbered list whole across blank lines", () => {
    const root = md("1. One\n\n2. Two\n\n3. Three");
    expect(root.querySelectorAll("ol")).toHaveLength(1);
    expect(root.querySelectorAll("ol > li")).toHaveLength(3);
  });

  it("renders a pipe table", () => {
    md("| Strain | Yield |\n|---|---:|\n| YL-A | 0.42 g/g |\n| YL-B | 0.31 g/g |");
    expect(screen.getByRole("columnheader", { name: "Strain" })).toBeInTheDocument();
    expect(screen.getAllByRole("row")).toHaveLength(3);
    expect(screen.getByRole("cell", { name: "0.31 g/g" })).toBeInTheDocument();
  });

  it("links only web addresses, and shows HTML as text", () => {
    md("[paper](https://doi.org/10.1/x) [bad](javascript:alert(1)) <b>not bold</b>");
    expect(screen.getByRole("link", { name: "paper" })).toHaveAttribute(
      "href",
      "https://doi.org/10.1/x",
    );
    expect(screen.queryByRole("link", { name: "bad" })).not.toBeInTheDocument();
    expect(screen.getByText(/<b>not bold<\/b>/)).toBeInTheDocument();
  });

  it("hands each citation handle to the page, one or several in a bracket", () => {
    const seen: string[] = [];
    md("Yield rose [E0123456789] and fell [Eaaaaaaaaaa, Ebbbbbbbbbb].", (handle, key) => {
      seen.push(handle);
      return <sup key={key}>{handle}</sup>;
    });
    expect(seen).toEqual(["E0123456789", "Eaaaaaaaaaa", "Ebbbbbbbbbb"]);
  });

  it("shows an unclosed mark as typed, as it is mid-stream", () => {
    md("Still **writ");
    expect(screen.getByText("Still **writ")).toBeInTheDocument();
  });
});

const ROW: SourceBlock = {
  handle: "E0000000001",
  title: "T",
  year: 2024,
  source_label: null,
  locator: "Results · table row",
  display_restricted: false,
  chunk_type: "table_row",
  section: "Results",
  snippet: "",
  text: "Table 2. Lipid titres — Strain: YL-A | Titre: 4.2 g/L",
};

describe("source formatting", () => {
  it("names three authors, then et al.", () => {
    expect(authorLine(["A X", "B Y"])).toBe("A X, B Y");
    expect(authorLine(["A X", "B Y", "C Z", "D W"])).toBe("A X, B Y, C Z et al.");
    expect(authorLine([])).toBe("");
  });

  it("links a PMC id whichever way it is written", () => {
    expect(pmcUrl("PMC123")).toBe("https://pmc.ncbi.nlm.nih.gov/articles/PMC123/");
    expect(pmcUrl("123")).toBe("https://pmc.ncbi.nlm.nih.gov/articles/PMC123/");
  });

  it("shows a table row as its cells again", () => {
    expect(passageOf(ROW)).toEqual({
      kind: "row",
      caption: "Table 2. Lipid titres",
      cells: [
        { label: "Strain", value: "YL-A" },
        { label: "Titre", value: "4.2 g/L" },
      ],
    });
    expect(whereLine(ROW)).toBe("Results · a table row");
  });

  it("joins words a PDF hyphenated across lines", () => {
    const passage = passageOf({ ...ROW, chunk_type: "paragraph", text: "lipid accu-\nmulation  rose\n\nNext." });
    expect(passage).toEqual({ kind: "text", paragraphs: ["lipid accumulation rose", "Next."] });
  });

  it("says where a passage is on its page", () => {
    expect(
      placeLines({ section_path: ["Results"], paragraph: 1, paragraphs: 4, page: 2, paragraph_on_page: 5, paragraphs_on_page: 5 }),
    ).toEqual([
      "The 1st of 4 paragraphs of Results.",
      "Page 2: the 5th of 5 paragraphs on the page, near the bottom.",
    ]);
  });

  it("marks the passage inside its paragraph, or nothing when it is not there", () => {
    expect(markPassage("One. Two three. Four.", "Two three.")).toEqual({
      before: "One. ",
      marked: "Two three.",
      after: " Four.",
    });
    expect(markPassage("One.", "Missing").marked).toBe("");
  });
});
