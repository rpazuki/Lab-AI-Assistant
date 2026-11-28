/**
 * How an Ask source reads: who wrote the paper, where it was published, links
 * to it, where in it the passage is, and the passage itself laid out.
 *
 * Pure functions of what the API sent, so the source card stays layout only.
 */

import type { PassagePlace, SourceBlock } from "@/types";

/** "Pazuki R, Ledesma-Amaro R, Smith J et al." Shows `shown` names at most. */
export function authorLine(authors: string[] | undefined, shown = 3): string {
  if (!authors || authors.length === 0) return "";
  if (authors.length <= shown) return authors.join(", ");
  return `${authors.slice(0, shown).join(", ")} et al.`;
}

function bare(value: string, prefixes: RegExp): string {
  return value.trim().replace(prefixes, "");
}

export function doiUrl(doi: string): string {
  return `https://doi.org/${bare(doi, /^(https?:\/\/(dx\.)?doi\.org\/|doi:)/i)}`;
}

export function pubmedUrl(pmid: string): string {
  return `https://pubmed.ncbi.nlm.nih.gov/${encodeURIComponent(bare(pmid, /^pmid:/i))}/`;
}

export function pmcUrl(pmcid: string): string {
  const id = bare(pmcid, /^pmcid?:/i);
  return `https://pmc.ncbi.nlm.nih.gov/articles/${encodeURIComponent(/^pmc/i.test(id) ? id.toUpperCase() : `PMC${id}`)}/`;
}

/** The links a reader can follow to the paper, best first. */
export function paperLinks(source: SourceBlock): { label: string; value: string; href: string }[] {
  const links: { label: string; value: string; href: string }[] = [];
  if (source.doi) links.push({ label: "DOI", value: bare(source.doi, /^doi:/i), href: doiUrl(source.doi) });
  if (source.pmid) links.push({ label: "PMID", value: source.pmid, href: pubmedUrl(source.pmid) });
  if (source.pmcid) links.push({ label: "PMC", value: source.pmcid, href: pmcUrl(source.pmcid) });
  return links;
}

/** Where the passage was taken from, as retrieval recorded it: "Results · page 4". */
export function whereLine(source: SourceBlock): string {
  const parts: string[] = [];
  if (source.chunk_type === "abstract") parts.push("Abstract");
  else if (source.section) parts.push(source.section);
  if (source.page !== null && source.page !== undefined) parts.push(`page ${source.page}`);
  if (source.chunk_type === "table_row") parts.push("a table row");
  else if (source.chunk_type === "figure_caption") parts.push("a figure caption");
  else if (source.chunk_type === "table_caption") parts.push("a table caption");
  if (parts.length === 0 && source.locator && source.locator !== "document") {
    return source.locator.replace(/\bp\.(\d+)/, "page $1");
  }
  return parts.join(" · ");
}

function ordinal(n: number): string {
  const tens = n % 100;
  if (tens >= 11 && tens <= 13) return `${n}th`;
  return `${n}${["th", "st", "nd", "rd"][n % 10] ?? "th"}`;
}

/** "near the top", "about halfway down", "near the bottom" of a page. */
function heightOnPage(position: number, count: number): string | null {
  if (count < 3) return null;
  const fraction = (position - 0.5) / count;
  if (fraction < 1 / 3) return "near the top";
  if (fraction < 2 / 3) return "about halfway down";
  return "near the bottom";
}

/** Where a cited passage sits, in sentences a reader can follow to it. */
export function placeLines(place: PassagePlace | undefined): string[] {
  if (!place) return [];
  const lines: string[] = [];
  const path = (place.section_path ?? []).join(" › ");
  if (place.table) {
    const table = place.table.label ?? "A table";
    const row = place.table.row !== null ? `, row ${place.table.row}` : "";
    lines.push(`${table}${row}${path ? `, in ${path}` : ""}.`);
  } else if (place.paragraph && place.paragraphs) {
    const where = path ? ` of ${path}` : "";
    lines.push(`The ${ordinal(place.paragraph)} of ${place.paragraphs} paragraphs${where}.`);
  } else if (path) {
    lines.push(`In ${path}.`);
  }
  if (place.page) {
    if (place.paragraph_on_page && place.paragraphs_on_page) {
      const height = heightOnPage(place.paragraph_on_page, place.paragraphs_on_page);
      lines.push(
        `Page ${place.page}: the ${ordinal(place.paragraph_on_page)} of ${place.paragraphs_on_page} paragraphs on the page${height ? `, ${height}` : ""}.`,
      );
    } else {
      lines.push(`Page ${place.page}.`);
    }
  }
  return lines;
}

/** Text as one readable run: no line-end hyphenation, single spaces. */
export function tidy(text: string): string {
  return text
    .replace(/(\w)-\n(\w)/g, "$1$2")
    .replace(/\s+/g, " ")
    .trim();
}

export type Passage =
  | { kind: "text"; paragraphs: string[] }
  | { kind: "row"; caption: string | null; cells: { label: string | null; value: string }[] };

/**
 * A passage laid out for reading. A table row was flattened for search as
 * "Caption — Header: value | Header: value"; it is shown as its cells again.
 */
export function passageOf(source: SourceBlock): Passage {
  const text = source.text || source.snippet || "";
  if (source.chunk_type === "table_row") {
    const dash = text.indexOf(" — ");
    const caption = dash >= 0 ? tidy(text.slice(0, dash)) : null;
    const row = dash >= 0 ? text.slice(dash + 3) : text;
    const cells = row
      .split(" | ")
      .map((cell) => {
        const colon = cell.indexOf(": ");
        return colon > 0 && colon < 60
          ? { label: tidy(cell.slice(0, colon)), value: tidy(cell.slice(colon + 2)) }
          : { label: null, value: tidy(cell) };
      })
      .filter((cell) => cell.value);
    return { kind: "row", caption, cells };
  }
  const paragraphs = text
    .replace(/\r\n?/g, "\n")
    .split(/\n\s*\n/)
    .map(tidy)
    .filter(Boolean);
  return { kind: "text", paragraphs };
}

/**
 * The paragraph a passage came from, split around the passage so it can be
 * marked. The passage is a window of the paragraph, so it is found by its
 * opening words; when it cannot be found, the whole paragraph is unmarked.
 */
export function markPassage(
  paragraph: string,
  passage: string,
): { before: string; marked: string; after: string } {
  const whole = tidy(paragraph);
  const part = tidy(passage).replace(/…$/, "");
  if (!part) return { before: whole, marked: "", after: "" };
  const opening = part.slice(0, 60);
  const start = whole.indexOf(opening);
  if (start < 0) return { before: whole, marked: "", after: "" };
  const end = Math.min(whole.length, start + part.length);
  return { before: whole.slice(0, start), marked: whole.slice(start, end), after: whole.slice(end) };
}
