/**
 * An answer to take elsewhere: Markdown with numbered citations and a reference
 * list, for a lab notebook, a draft or an email.
 *
 * Citations are renumbered in the order the answer first uses them, as a paper
 * numbers its references, so the list has no gaps. On the page a citation's
 * number is its source's place in the sources list instead, which is why the two
 * can differ. A handle the server never supplied becomes [?], and the export says
 * so: the warning goes with the text.
 */

import type { SourceBlock } from "@/types";

const HANDLE = /\[(E[0-9a-f]{10}(?:\s*[,;]\s*E[0-9a-f]{10})*)\]/g;

export interface ExportableAnswer {
  question: string;
  answer: string;
  sources: SourceBlock[];
  releaseLabel: string | null;
  unsupported: string[];
}

/** A reference as Vancouver style writes it, from what the source knows. */
export function reference(source: SourceBlock): string {
  const authors = source.authors ?? [];
  const names =
    authors.length > 6 ? `${authors.slice(0, 6).join(", ")}, et al.` : authors.join(", ");
  const parts: string[] = [];
  if (names) parts.push(names.endsWith(".") ? names : `${names}.`);
  const title = (source.title ?? "Untitled").trim();
  parts.push(/[.?!]$/.test(title) ? title : `${title}.`);
  if (source.journal) parts.push(`${source.journal}.`);
  if (source.year) parts.push(`${source.year}.`);
  if (source.doi) parts.push(`doi:${source.doi.replace(/^doi:/i, "")}.`);
  if (source.pmid) parts.push(`PMID: ${source.pmid}.`);
  if (source.pmcid) parts.push(`PMCID: ${source.pmcid}.`);
  if (source.abstract_only) parts.push("(Abstract only.)");
  return parts.join(" ");
}

/** The answer with its citations numbered, and the sources it cited in that order. */
export function numberCitations(
  answer: string,
  sources: SourceBlock[],
): { text: string; cited: SourceBlock[]; unknown: boolean } {
  const byHandle = new Map(sources.map((source) => [source.handle, source]));
  const order: string[] = [];
  let unknown = false;
  const text = answer.replace(HANDLE, (_whole, group: string) => {
    const numbers = group.split(/\s*[,;]\s*/).map((handle) => {
      if (!byHandle.has(handle)) {
        unknown = true;
        return "?";
      }
      if (!order.includes(handle)) order.push(handle);
      return String(order.indexOf(handle) + 1);
    });
    return `[${numbers.join(", ")}]`;
  });
  return { text, cited: order.map((handle) => byHandle.get(handle)!), unknown };
}

export function answerAsMarkdown(turn: ExportableAnswer, asOf: Date = new Date()): string {
  const { text, cited, unknown } = numberCitations(turn.answer, turn.sources);
  const lines = [`## ${turn.question.trim()}`, "", text.trim(), ""];
  if (unknown || turn.unsupported.length > 0) {
    lines.push(
      "> Citations marked [?] name a source the system did not supply. Do not rely on them.",
      "",
    );
  }
  if (cited.length > 0) {
    lines.push("### References", "");
    cited.forEach((source, index) => lines.push(`${index + 1}. ${reference(source)}`));
    lines.push("");
  }
  const release = turn.releaseLabel ? `, release ${turn.releaseLabel}` : "";
  lines.push(`_From the lab corpus${release}, ${asOf.toISOString().slice(0, 10)}._`, "");
  return lines.join("\n");
}

/** A file name from the question: "what-lipid-yield-was-reported.md". */
export function exportFileName(question: string): string {
  const slug = question
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60)
    .replace(/-+$/, "");
  return `${slug || "answer"}.md`;
}
