/**
 * Light Markdown for answers: what a model writes when it structures an answer.
 *
 * Headings, paragraphs, line breaks, bullet and numbered lists (nested by
 * indentation), quotes, code, rules and pipe tables; inside a line, bold,
 * italics, code, links and bare web addresses. Citation handles such as
 * [E1a2b3c4d5] go to `renderCitation`, so the page can turn them into numbers
 * that open the source.
 *
 * It builds React elements and never HTML strings, so text in an answer (which
 * passed through a model that read untrusted papers) cannot add markup to the
 * page: an HTML tag in an answer shows as the text it is. Only http and https
 * links are made; anything else stays text.
 *
 * It renders whatever has arrived. Mid-stream, an unclosed `**` shows as typed
 * until its closing pair arrives, and then the line becomes bold.
 */

import { Fragment, type ReactNode } from "react";

export type CitationRenderer = (handle: string, key: string) => ReactNode;

const HANDLE = "E[0-9a-f]{10}";

interface InlinePattern {
  regex: RegExp;
  render: (match: RegExpExecArray, key: string, cite: CitationRenderer) => ReactNode;
}

function safeHref(url: string): string | null {
  return /^https?:\/\//i.test(url) ? url : null;
}

const INLINE: InlinePattern[] = [
  {
    regex: /`([^`\n]+)`/,
    render: (m, key) => (
      <code key={key} className="rounded bg-surface-subtle px-1 py-0.5 font-mono text-[0.85em]">
        {m[1]}
      </code>
    ),
  },
  {
    // One handle, or several in one pair of brackets: [Eaaa, Ebbb].
    regex: new RegExp(`\\[(${HANDLE}(?:\\s*[,;]\\s*${HANDLE})*)\\]`),
    render: (m, key, cite) => (
      <Fragment key={key}>
        {(m[1] ?? "").split(/\s*[,;]\s*/).map((handle, index) => cite(handle, `${key}-${index}`))}
      </Fragment>
    ),
  },
  {
    regex: /\[([^\]\n]+)\]\((\S+?)\)/,
    render: (m, key, cite) => {
      const href = safeHref(m[2] ?? "");
      const label = inline(m[1] ?? "", `${key}-l`, cite);
      return href ? (
        <a key={key} href={href} target="_blank" rel="noreferrer" className="text-link underline">
          {label}
        </a>
      ) : (
        <Fragment key={key}>{label}</Fragment>
      );
    },
  },
  {
    regex: /\*\*(?=\S)([^\n]+?)(?<=\S)\*\*|__(?=\S)([^\n]+?)(?<=\S)__/,
    render: (m, key, cite) => (
      <strong key={key} className="font-semibold text-heading">
        {inline(m[1] ?? m[2] ?? "", `${key}-b`, cite)}
      </strong>
    ),
  },
  {
    regex: /\*(?=[^\s*])([^\n*]+?)(?<=[^\s*])\*|(?<![\w])_(?=\S)([^\n_]+?)(?<=\S)_(?![\w])/,
    render: (m, key, cite) => <em key={key}>{inline(m[1] ?? m[2] ?? "", `${key}-i`, cite)}</em>,
  },
  {
    regex: /https?:\/\/[^\s<>()[\]]+[^\s<>()[\].,;:!?'"]/,
    render: (m, key) => (
      <a key={key} href={m[0]} target="_blank" rel="noreferrer" className="break-all text-link underline">
        {m[0]}
      </a>
    ),
  },
];

/** One line's text with its inline marks. */
function inline(text: string, key: string, cite: CitationRenderer): ReactNode[] {
  const out: ReactNode[] = [];
  let rest = text;
  let index = 0;
  while (rest) {
    let best: { match: RegExpExecArray; pattern: InlinePattern } | null = null;
    for (const pattern of INLINE) {
      const match = pattern.regex.exec(rest);
      if (match && (best === null || match.index < best.match.index)) best = { match, pattern };
    }
    if (best === null) {
      out.push(rest);
      break;
    }
    if (best.match.index > 0) out.push(rest.slice(0, best.match.index));
    out.push(best.pattern.render(best.match, `${key}.${index++}`, cite));
    rest = rest.slice(best.match.index + best.match[0].length);
  }
  return out;
}

/** Lines joined by visible breaks: a model's single line break is meant. */
function lines(text: string[], key: string, cite: CitationRenderer): ReactNode[] {
  return text.flatMap((line, index) => [
    ...(index > 0 ? [<br key={`${key}-br${index}`} />] : []),
    ...inline(line, `${key}-${index}`, cite),
  ]);
}

const FENCE = /^\s*```/;
const HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const RULE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
const QUOTE = /^\s{0,3}>\s?(.*)$/;
const ITEM = /^(\s*)([-*+•]|\d{1,3}[.)])\s+(.*)$/;
const TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function isTableRow(line: string | undefined): boolean {
  return line !== undefined && line.includes("|");
}

function cells(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

function startsBlock(line: string, next: string | undefined): boolean {
  return (
    FENCE.test(line) ||
    HEADING.test(line) ||
    RULE.test(line) ||
    QUOTE.test(line) ||
    ITEM.test(line) ||
    (isTableRow(line) && next !== undefined && TABLE_RULE.test(next))
  );
}

interface ListItem {
  text: string[];
  children: ListItem[];
  ordered: boolean;
  start: number;
}

/** A run of list lines, nested by indentation, as a tree of items. */
function parseList(source: string[]): { items: ListItem[]; ordered: boolean; start: number } {
  const root: ListItem = { text: [], children: [], ordered: false, start: 1 };
  const stack: { indent: number; item: ListItem }[] = [{ indent: -1, item: root }];
  for (const line of source) {
    const match = ITEM.exec(line);
    if (match) {
      const marker = match[2] ?? "-";
      const indent = (match[1] ?? "").replace(/\t/g, "    ").length;
      const ordered = /\d/.test(marker);
      const item: ListItem = {
        text: [match[3] ?? ""],
        children: [],
        ordered,
        start: ordered ? parseInt(marker, 10) : 1,
      };
      while (stack.length > 1 && stack.at(-1)!.indent >= indent) stack.pop();
      stack.at(-1)!.item.children.push(item);
      stack.push({ indent, item });
    } else if (stack.length > 1) {
      stack.at(-1)!.item.text.push(line.trim());
    }
  }
  const first = root.children[0];
  return { items: root.children, ordered: first?.ordered ?? false, start: first?.start ?? 1 };
}

function renderList(
  items: ListItem[],
  ordered: boolean,
  start: number,
  key: string,
  cite: CitationRenderer,
): ReactNode {
  const body = items.map((item, index) => (
    <li key={`${key}-${index}`} className="pl-1">
      {lines(item.text, `${key}-${index}`, cite)}
      {item.children.length > 0
        ? renderList(
            item.children,
            item.children[0]!.ordered,
            item.children[0]!.start,
            `${key}-${index}-sub`,
            cite,
          )
        : null}
    </li>
  ));
  return ordered ? (
    <ol key={key} start={start} className="list-decimal space-y-1 pl-6">
      {body}
    </ol>
  ) : (
    <ul key={key} className="list-disc space-y-1 pl-6">
      {body}
    </ul>
  );
}

function blocks(source: string[], key: string, cite: CitationRenderer): ReactNode[] {
  const at = (index: number): string => source[index] ?? "";
  const out: ReactNode[] = [];
  let i = 0;
  let n = 0;
  const next = () => `${key}-${n++}`;
  while (i < source.length) {
    const line = at(i);
    if (!line.trim()) {
      i++;
      continue;
    }
    if (FENCE.test(line)) {
      const code: string[] = [];
      i++;
      while (i < source.length && !FENCE.test(at(i))) code.push(at(i++));
      i++;
      out.push(
        <pre
          key={next()}
          className="overflow-x-auto rounded-control border border-border bg-surface-subtle p-3 font-mono text-xs"
        >
          <code>{code.join("\n")}</code>
        </pre>,
      );
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      const level = (heading[1] ?? "#").length;
      const content = inline(heading[2] ?? "", next(), cite);
      const className =
        level <= 2
          ? "pt-1 text-base font-semibold text-heading"
          : "pt-1 text-sm font-semibold text-heading";
      out.push(
        level <= 2 ? (
          <h3 key={next()} className={className}>
            {content}
          </h3>
        ) : (
          <h4 key={next()} className={className}>
            {content}
          </h4>
        ),
      );
      i++;
      continue;
    }
    if (RULE.test(line)) {
      out.push(<hr key={next()} className="border-border" />);
      i++;
      continue;
    }
    if (isTableRow(line) && TABLE_RULE.test(at(i + 1))) {
      const head = cells(line);
      const rows: string[][] = [];
      i += 2;
      while (i < source.length && isTableRow(at(i)) && at(i).trim()) {
        rows.push(cells(at(i++)));
      }
      const tableKey = next();
      out.push(
        <div key={tableKey} className="overflow-x-auto">
          <table className="data-table text-sm">
            <thead>
              <tr>
                {head.map((cell, c) => (
                  <th key={c} scope="col">
                    {inline(cell, `${tableKey}-h${c}`, cite)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, r) => (
                <tr key={r}>
                  {head.map((_, c) => (
                    <td key={c}>{inline(row[c] ?? "", `${tableKey}-${r}-${c}`, cite)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }
    if (QUOTE.test(line)) {
      const quoted: string[] = [];
      while (i < source.length && QUOTE.test(at(i))) quoted.push(QUOTE.exec(at(i++))?.[1] ?? "");
      const quoteKey = next();
      out.push(
        <blockquote key={quoteKey} className="space-y-2 border-l-2 border-border-strong pl-3 text-muted">
          {blocks(quoted, quoteKey, cite)}
        </blockquote>,
      );
      continue;
    }
    if (ITEM.test(line)) {
      const listLines: string[] = [];
      while (i < source.length) {
        const current = at(i);
        if (!current.trim()) {
          // A blank line inside a list continues it only if the list goes on.
          if (ITEM.test(at(i + 1)) || /^\s{2,}\S/.test(at(i + 1))) {
            i++;
            continue;
          }
          break;
        }
        if (!ITEM.test(current) && startsBlock(current, at(i + 1))) break;
        listLines.push(current);
        i++;
      }
      const list = parseList(listLines);
      out.push(renderList(list.items, list.ordered, list.start, next(), cite));
      continue;
    }
    const paragraph: string[] = [];
    while (i < source.length && at(i).trim() && !startsBlock(at(i), at(i + 1))) {
      paragraph.push(at(i++).trim());
    }
    if (paragraph.length === 0) {
      // A line that looks like a block start but is not one (a lone "|"): text.
      paragraph.push(at(i++).trim());
    }
    const paragraphKey = next();
    out.push(<p key={paragraphKey}>{lines(paragraph, paragraphKey, cite)}</p>);
  }
  return out;
}

const plainCitation: CitationRenderer = (handle, key) => (
  <span key={key} className="font-mono text-xs text-muted">
    [{handle}]
  </span>
);

export function Markdown({
  text,
  renderCitation = plainCitation,
  className = "",
}: {
  text: string;
  renderCitation?: CitationRenderer;
  className?: string;
}) {
  const source = text.replace(/\r\n?/g, "\n").split("\n");
  return (
    <div className={`space-y-3 text-sm leading-relaxed text-ink ${className}`}>
      {blocks(source, "md", renderCitation)}
    </div>
  );
}
