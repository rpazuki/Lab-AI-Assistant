"use client";

/**
 * One source of an answer: the paper (title, authors, journal, year, links),
 * where in it the passage was found, and the passage, laid out to be read.
 *
 * A cited source opens **in context**: the whole paragraph with the passage
 * marked, where it sits in the paper (section, paragraph, page), and the release
 * it came from. The opening is fetched only when asked for.
 */

import { forwardRef, useState } from "react";

import { Icon } from "@/components/ui/icons";
import { StateChip } from "@/components/ui/primitives";
import {
  authorLine,
  doiUrl,
  markPassage,
  paperLinks,
  passageOf,
  placeLines,
  tidy,
  whereLine,
} from "@/lib/sources";
import type { CitationContext, CitationStatus, SourceBlock } from "@/types";

export type ContextState =
  | { state: "loading" }
  | { state: "open"; context: CitationContext }
  | { state: "failed"; reason: string };

const STATUS_NOTE: Partial<Record<CitationStatus, string>> = {
  superseded: "The cited passage has since been replaced in the corpus.",
  rights_withdrawn: "The rights to show this passage were withdrawn.",
  legacy_unverified: "Cited before citations were verified.",
  not_supplied: "Not one of the sources supplied: do not trust this citation.",
};

function Passage({ source }: { source: SourceBlock }) {
  const [full, setFull] = useState(false);
  const passage = passageOf(source);
  if (passage.kind === "row") {
    return (
      <div className="space-y-1 border-l-2 border-accent pl-3 text-xs text-ink">
        {passage.caption ? <p className="font-medium text-heading">{passage.caption}</p> : null}
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5">
          {passage.cells.map((cell, index) =>
            cell.label ? (
              <div key={index} className="contents">
                <dt className="text-muted">{cell.label}</dt>
                <dd>{cell.value}</dd>
              </div>
            ) : (
              <dd key={index} className="col-span-2">
                {cell.value}
              </dd>
            ),
          )}
        </dl>
      </div>
    );
  }
  if (passage.paragraphs.length === 0) return null;
  const long = passage.paragraphs.join(" ").length > 420;
  return (
    <div className="space-y-1">
      <div
        className={`space-y-2 border-l-2 border-accent pl-3 text-xs leading-relaxed text-ink ${
          long && !full ? "line-clamp-6" : ""
        }`}
      >
        {passage.paragraphs.map((paragraph, index) => (
          <p key={index}>{paragraph}</p>
        ))}
      </div>
      {long ? (
        <button
          type="button"
          className="pl-3 text-xs text-link underline hover:no-underline"
          onClick={() => setFull((value) => !value)}
          aria-expanded={full}
        >
          {full ? "Show less" : "Show the whole passage"}
        </button>
      ) : null}
    </div>
  );
}

function InContext({ source, opened }: { source: SourceBlock; opened: ContextState }) {
  if (opened.state === "loading") {
    return <p className="text-xs text-muted">Opening the passage in its paper…</p>;
  }
  if (opened.state === "failed") {
    return <p className="text-xs text-danger-strong">{opened.reason}</p>;
  }
  const context = opened.context;
  if (!context.resolved) {
    return (
      <p className="text-xs text-warning-strong">
        {context.reason ?? "This citation could not be opened."}
      </p>
    );
  }
  const place = placeLines(context.place);
  const cells = (context.nodes ?? []).filter((node) => node.kind === "cell");
  const paragraph =
    context.paragraph ??
    (context.nodes ?? [])
      .filter((node) => node.kind !== "cell")
      .map((node) => node.text)
      .join("\n\n");
  const passage = source.text || source.snippet || context.span?.quote_preview || "";
  const marked = paragraph ? markPassage(paragraph, context.span?.quote_preview || passage) : null;
  return (
    <div className="space-y-2 text-xs">
      {place.length > 0 ? (
        <div>
          <p className="font-semibold text-heading">Where in the paper</p>
          {place.map((line) => (
            <p key={line} className="text-ink">
              {line}
            </p>
          ))}
        </div>
      ) : null}
      {marked ? (
        <div>
          <p className="font-semibold text-heading">The paragraph</p>
          <p className="leading-relaxed text-ink">
            {marked.before}
            {marked.marked ? (
              <mark className="rounded-sm bg-success-lt px-0.5 text-ink">{marked.marked}</mark>
            ) : null}
            {marked.after}
          </p>
        </div>
      ) : null}
      {cells.length > 0 ? (
        <div>
          <p className="font-semibold text-heading">The cited cells</p>
          <ul className="list-disc pl-5 text-ink">
            {cells.map((cell) => (
              <li key={cell.node_id}>{tidy(cell.text)}</li>
            ))}
          </ul>
        </div>
      ) : null}
      <p className="text-muted">
        {context.span ? "A verified quotation" : "Read from the paper's parsed text"}
        {context.release_label ? `, release ${context.release_label}` : ""}.
      </p>
    </div>
  );
}

export const SourceCard = forwardRef<
  HTMLLIElement,
  {
    source: SourceBlock;
    number: number;
    /** The citation of this source in the answer, if the answer cited it. */
    citation?: { status: CitationStatus } | null;
    opened?: ContextState;
    highlighted?: boolean;
    onOpen?: () => void;
    onClose?: () => void;
  }
>(function SourceCard({ source, number, citation, opened, highlighted, onOpen, onClose }, ref) {
  const meta = [source.journal, source.year].filter(Boolean).join(" · ");
  const authors = authorLine(source.authors);
  const where = whereLine(source);
  const links = paperLinks(source);
  const titleHref = source.doi ? doiUrl(source.doi) : links[0]?.href;
  const statusNote = citation && citation.status !== "valid" ? STATUS_NOTE[citation.status] : null;

  return (
    <li
      ref={ref}
      id={`source-${source.handle}`}
      className={`scroll-mt-4 space-y-2 px-5 py-4 transition-colors ${
        highlighted ? "bg-success-lt/60" : ""
      }`}
    >
      <div className="flex items-start gap-3">
        <span
          title={source.handle}
          className={`mt-0.5 inline-flex h-6 min-w-6 shrink-0 items-center justify-center rounded-badge px-1 text-xs font-semibold ${
            citation ? "bg-primary text-on-primary" : "border border-border-strong text-muted"
          }`}
        >
          {number}
        </span>
        <div className="min-w-0 space-y-1">
          <p className="text-sm font-semibold leading-snug text-heading">
            {titleHref ? (
              <a href={titleHref} target="_blank" rel="noreferrer" className="hover:underline">
                {source.title ?? "Untitled"}
              </a>
            ) : (
              (source.title ?? "Untitled")
            )}
          </p>
          {authors ? (
            <p className="text-xs text-ink" title={(source.authors ?? []).join(", ")}>
              {authors}
            </p>
          ) : null}
          {meta ? <p className="text-xs italic text-muted">{meta}</p> : null}
          {links.length > 0 ? (
            <p className="flex flex-wrap gap-x-3 gap-y-1 text-xs">
              {links.map((link) => (
                <a
                  key={link.label}
                  href={link.href}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 text-link underline hover:no-underline"
                >
                  <span className="font-medium">{link.label}</span> {link.value}
                  <Icon name="external" className="h-3 w-3" />
                </a>
              ))}
            </p>
          ) : null}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
        {where ? <span>{where}</span> : null}
        {source.abstract_only ? <StateChip state="abstract_only" kind="flag" /> : null}
        {source.display_restricted ? <StateChip state="excerpt_only" kind="flag" /> : null}
        {!citation ? <span className="ml-auto">Not cited in the answer</span> : null}
      </div>

      {source.withheld ? (
        <p className="text-xs text-warning-strong">
          This installation no longer serves this paper, so its passage is not shown.
        </p>
      ) : source.from_citation ? (
        <p className="text-xs text-muted">
          This answer predates kept sources. Open it in context to read the cited passage.
        </p>
      ) : (
        <Passage source={source} />
      )}
      {statusNote ? <p className="text-xs text-warning-strong">{statusNote}</p> : null}

      {citation && !source.withheld && onOpen ? (
        <div className="space-y-2">
          <button
            type="button"
            className="text-xs font-medium text-link underline hover:no-underline"
            aria-expanded={Boolean(opened)}
            onClick={() => (opened ? onClose?.() : onOpen())}
          >
            {opened ? "Hide the context" : "Open in context"}
          </button>
          {opened ? (
            <div className="rounded-control border border-border bg-surface-subtle p-3">
              <InContext source={source} opened={opened} />
            </div>
          ) : null}
        </div>
      ) : null}
    </li>
  );
});
