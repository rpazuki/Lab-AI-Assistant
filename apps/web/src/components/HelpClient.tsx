"use client";

import Link from "next/link";
import { useEffect, useMemo, useState, type ReactNode } from "react";

import { Icon } from "@/components/ui/icons";
import { Input, StateChip } from "@/components/ui/primitives";
import { getMe } from "@/lib/api";
import { HELP_TOPICS, searchHelp, type HelpTopic, type HowTo } from "@/lib/help";
import { statusGroup, type StatusKind } from "@/lib/statuses";

/**
 * Inline emphasis in help text: `**bold**` for the names on screen, `*italic*` for
 * species. Nothing else is interpreted, so help text cannot inject markup.
 */
export function rich(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*|\*[^*]+\*)/g).map((part, index) => {
    if (part.startsWith("**") && part.endsWith("**")) {
      return (
        <strong key={index} className="font-semibold text-heading">
          {part.slice(2, -2)}
        </strong>
      );
    }
    if (part.startsWith("*") && part.endsWith("*") && part.length > 2) {
      return <em key={index}>{part.slice(1, -1)}</em>;
    }
    return part;
  });
}

export const anchor = (topic: string, section: string) => `${topic}--${section}`;
export const guideAnchor = (title: string) =>
  title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");

/** Which topics the signed-in member's role opens; `null` while asking. */
function useCapabilities(): Set<string> | null {
  const [held, setHeld] = useState<Set<string> | null>(null);
  useEffect(() => {
    getMe()
      .then((me) => setHeld(new Set(me.capabilities)))
      .catch(() => setHeld(new Set()));
  }, []);
  return held;
}

function opens(topic: HelpTopic, held: Set<string> | null): boolean | null {
  if (held === null) return null;
  return topic.anyOf.length === 0 || topic.anyOf.some((capability) => held.has(capability));
}

/** The index: every topic as a card, and a search over all guides. */
export function HelpIndex() {
  const [query, setQuery] = useState("");
  const held = useCapabilities();
  const results = useMemo(() => searchHelp(query), [query]);

  return (
    <div className="space-y-6">
      <div className="max-w-xl">
        <label className="block space-y-1.5 text-sm">
          <span className="block font-medium text-ink">Search the guides</span>
          <Input
            type="search"
            value={query}
            placeholder="for example: invite, upload pdf, publish, token limit"
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
      </div>

      {query.trim() ? (
        <section aria-label="Search results" className="space-y-2">
          <p className="text-sm text-muted">
            {results.length} result{results.length === 1 ? "" : "s"}
          </p>
          <ul className="divide-y divide-border overflow-hidden rounded-card border border-border bg-surface shadow-card">
            {results.map(({ topic, section, howTo }) => (
              <li key={`${topic.id}:${section.id}:${howTo?.title ?? ""}`}>
                <Link
                  href={`/help/${topic.id}#${howTo ? guideAnchor(howTo.title) : anchor(topic.id, section.id)}`}
                  className="block px-5 py-3 hover:bg-surface-subtle"
                >
                  <span className="block text-sm font-semibold text-link">
                    {howTo ? howTo.title : section.title}
                  </span>
                  <span className="block text-xs text-muted">
                    {topic.title} › {section.title}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
          {results.length === 0 ? (
            <p className="text-sm text-muted">Nothing matches. Try fewer or different words.</p>
          ) : null}
        </section>
      ) : (
        <ul className="grid gap-4 @3xl:grid-cols-2 @6xl:grid-cols-3">
          {HELP_TOPICS.map((topic) => {
            const yours = opens(topic, held);
            return (
              <li key={topic.id}>
                <Link
                  href={`/help/${topic.id}`}
                  className="flex h-full flex-col gap-2 rounded-card border border-border bg-surface p-5 shadow-card transition-colors hover:border-border-strong"
                >
                  <span className="flex items-center gap-2 text-card-title font-semibold text-heading">
                    <span className="flex h-8 w-8 items-center justify-center rounded-control bg-success-lt text-success-strong">
                      <Icon name={topic.icon} />
                    </span>
                    {topic.title}
                  </span>
                  <span className="text-sm text-ink">{topic.summary}</span>
                  <span className="mt-auto text-xs text-muted">
                    {topic.audience}
                    {yours === false ? " · not part of your role" : ""}
                  </span>
                  <span className="text-xs text-muted">
                    {topic.sections.reduce((count, section) => count + section.howTos.length, 0)} how-to
                    guides
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/** Every value of one status group and what it means, from the shared glossary. */
function StatusTable({ kind }: { kind: StatusKind }) {
  const group = statusGroup(kind);
  return (
    <table className="data-table rounded-card border border-border bg-surface">
      <caption className="pb-1.5 text-left text-sm font-semibold text-heading">{group.title}</caption>
      <thead>
        <tr>
          <th scope="col">Status</th>
          <th scope="col">What it means</th>
        </tr>
      </thead>
      <tbody>
        {Object.entries(group.values).map(([value, meaning]) => (
          <tr key={value}>
            <td className="whitespace-nowrap align-top">
              <StateChip state={value} />
            </td>
            <td className="text-sm text-ink">{meaning}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Guide({ howTo }: { howTo: HowTo }) {
  return (
    <article
      id={guideAnchor(howTo.title)}
      aria-labelledby={`${guideAnchor(howTo.title)}-title`}
      className="scroll-mt-6 rounded-card border border-border bg-surface shadow-card"
    >
      <header className="border-b border-border px-5 py-3">
        <h4 id={`${guideAnchor(howTo.title)}-title`} className="text-sm font-semibold text-heading">
          {howTo.title}
        </h4>
        {howTo.needs ? (
          <p className="mt-0.5 text-xs text-muted">
            <span className="font-medium">You need:</span> {rich(howTo.needs)}
          </p>
        ) : null}
      </header>
      <ol className="list-decimal space-y-1.5 px-5 py-4 pl-10 text-sm text-ink marker:font-semibold marker:text-muted">
        {howTo.steps.map((step, index) => (
          <li key={index}>{rich(step)}</li>
        ))}
      </ol>
      {howTo.note ? (
        <p className="mx-5 mb-4 rounded-control border border-l-4 border-info-strong/25 bg-info-lt px-3 py-2 text-xs text-info-strong">
          <span className="font-semibold">Note: </span>
          {rich(howTo.note)}
        </p>
      ) : null}
    </article>
  );
}

/**
 * One topic, documentation-site style: every topic on the left, the article in the
 * middle, and "On this page" on the right. Each section explains what it is for,
 * then gives its how-to guides.
 */
export function HelpArticle({ topic }: { topic: HelpTopic }) {
  const held = useCapabilities();
  const yours = opens(topic, held);

  return (
    <div className="grid items-start gap-8 @4xl:grid-cols-[12rem_minmax(0,1fr)] @5xl:grid-cols-[12rem_minmax(0,1fr)_12rem]">
      <nav aria-label="Guides" className="hidden text-sm @4xl:sticky @4xl:top-6 @4xl:block">
        <Link href="/help" className="mb-3 block text-pretitle font-semibold uppercase tracking-wider text-muted hover:text-heading">
          All guides
        </Link>
        <ul className="space-y-0.5">
          {HELP_TOPICS.map((item) => (
            <li key={item.id}>
              <Link
                href={`/help/${item.id}`}
                aria-current={item.id === topic.id ? "page" : undefined}
                className={`flex items-center gap-2 rounded-control border-l-2 px-2 py-1.5 ${
                  item.id === topic.id
                    ? "border-accent bg-surface font-semibold text-heading"
                    : "border-transparent text-ink hover:bg-surface"
                }`}
              >
                <Icon name={item.icon} className="h-3.5 w-3.5" />
                {item.title}
              </Link>
            </li>
          ))}
        </ul>
      </nav>

      <div className="min-w-0 space-y-8">
        <header className="space-y-2">
          <p className="text-xs text-muted">
            <Link href="/help" className="text-link hover:underline">
              How-to guides
            </Link>{" "}
            › {topic.title}
          </p>
          <p className="max-w-3xl text-sm text-ink">{rich(topic.summary)}</p>
          <p className="flex flex-wrap items-center gap-3 text-xs text-muted">
            <span>
              <span className="font-medium">For:</span> {topic.audience}
            </span>
            {topic.href ? (
              <Link href={topic.href} className="inline-flex items-center gap-1 text-link hover:underline">
                <Icon name={topic.icon === "help" ? "ask" : topic.icon} className="h-3.5 w-3.5" />
                Open {topic.title}
              </Link>
            ) : null}
          </p>
          {yours === false ? (
            <p role="status" className="rounded-control border border-l-4 border-warning-strong/25 bg-warning-lt px-3 py-2 text-xs text-warning-strong">
              Your role does not include this page. The guide is here so you know what it does;
              ask an admin if you need it.
            </p>
          ) : null}
        </header>

        {topic.sections.map((section) => (
          <section
            key={section.id}
            id={anchor(topic.id, section.id)}
            aria-labelledby={`${anchor(topic.id, section.id)}-title`}
            className="scroll-mt-6 space-y-4"
          >
            <h2 id={`${anchor(topic.id, section.id)}-title`} className="border-b border-border pb-2 text-page-title font-semibold text-heading">
              {section.title}
            </h2>
            <div className="max-w-3xl space-y-3">
              <h3 className="text-pretitle font-semibold uppercase tracking-wider text-muted">What it is for</h3>
              {section.about.map((paragraph, index) => (
                <p key={index} className="text-sm leading-6 text-ink">
                  {rich(paragraph)}
                </p>
              ))}
            </div>
            <div className="max-w-3xl space-y-3">
              <h3 className="text-pretitle font-semibold uppercase tracking-wider text-muted">How to</h3>
              {section.howTos.map((howTo) => (
                <Guide key={howTo.title} howTo={howTo} />
              ))}
            </div>
            {section.statuses?.length ? (
              <div className="max-w-3xl space-y-3">
                <h3 className="text-pretitle font-semibold uppercase tracking-wider text-muted">Statuses</h3>
                {section.statuses.map((kind) => (
                  <StatusTable key={kind} kind={kind} />
                ))}
              </div>
            ) : null}
          </section>
        ))}
      </div>

      <nav aria-label="On this page" className="hidden text-xs @5xl:sticky @5xl:top-6 @5xl:block">
        <p className="mb-2 text-pretitle font-semibold uppercase tracking-wider text-muted">On this page</p>
        <ul className="space-y-2">
          {topic.sections.map((section) => (
            <li key={section.id}>
              <a href={`#${anchor(topic.id, section.id)}`} className="font-medium text-ink hover:text-link">
                {section.title}
              </a>
              <ul className="mt-1 space-y-1 border-l border-border pl-2">
                {section.howTos.map((howTo) => (
                  <li key={howTo.title}>
                    <a href={`#${guideAnchor(howTo.title)}`} className="text-muted hover:text-link">
                      {howTo.title.replace(/^How to /, "")}
                    </a>
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      </nav>
    </div>
  );
}
