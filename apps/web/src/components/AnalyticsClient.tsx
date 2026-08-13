"use client";

import { useEffect, useState } from "react";

import { Empty, Panel } from "@/components/ui/primitives";
import { corpusStats, journals, sourceContribution, subjectTerms, temporal } from "@/lib/api";
import type { CorpusStats } from "@/types";

/**
 * Corpus analytics.
 *
 * Every number is anchored to a release and filtered by the same policy as
 * retrieval — including the chunk total, because a count that quietly included
 * material the reader cannot open would itself be a disclosure.
 *
 * How much is withheld is shown to stewards as a number and to everyone else as
 * "restricted": a member needs to know their view is filtered so they can ask for
 * access, and does not need the size of what they are missing.
 */
export function AnalyticsClient() {
  const [stats, setStats] = useState<CorpusStats | null>(null);
  const [years, setYears] = useState<{ year: number; count: number }[]>([]);
  const [venues, setVenues] = useState<{ journal: string; count: number }[]>([]);
  const [terms, setTerms] = useState<{ term: string; count: number }[]>([]);
  const [sources, setSources] = useState<
    { connector_id: string; works: number; unique_contribution: number }[]
  >([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const [nextStats, nextYears, nextVenues, nextTerms, nextSources] = await Promise.all([
          corpusStats(),
          temporal(),
          journals(),
          subjectTerms(),
          sourceContribution(),
        ]);
        setStats(nextStats);
        setYears(nextYears);
        setVenues(nextVenues);
        setTerms(nextTerms);
        setSources(nextSources);
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : "analytics could not be loaded");
      }
    })();
  }, []);

  if (error) {
    return (
      <p role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">
        {error}
      </p>
    );
  }
  if (!stats) return <Empty>Loading…</Empty>;

  return (
    <div className="space-y-4">
      <Panel title={`Release ${stats.release_name}@${stats.release_version}`}>
        <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-5">
          {[
            ["Works", stats.works],
            ["Renditions", stats.renditions],
            ["Claims", stats.claims],
            ["Chunks", stats.chunks],
            // Stewards see how much is withheld; everyone else sees that some of
            // the release is withheld, which is what they need to ask for access.
            [
              "Withheld by policy",
              stats.withheld_by_policy ?? "restricted",
            ],
          ].map(([label, value]) => (
            <div key={String(label)}>
              <dt className="text-xs uppercase tracking-wide text-neutral-500">{label}</dt>
              <dd className="text-lg font-semibold">{value}</dd>
            </div>
          ))}
        </dl>
        <p className="mt-2 text-xs text-neutral-500">
          Published {stats.published_at ?? "not yet"}. Counts follow the same access policy as
          retrieval.
        </p>
      </Panel>

      <div className="grid gap-4 md:grid-cols-2">
        <Panel title="Publication years">
          {years.length === 0 ? (
            <Empty>No dated works in this release.</Empty>
          ) : (
            <ul className="space-y-1 text-sm">
              {years.map((row) => (
                <li key={row.year} className="flex items-center gap-2">
                  <span className="w-12 font-mono text-xs">{row.year}</span>
                  <span
                    className="h-3 rounded bg-[var(--accent)]"
                    style={{ width: `${Math.min(100, row.count * 12)}%` }}
                    aria-hidden
                  />
                  <span className="text-xs text-neutral-500">{row.count}</span>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel title="Venues">
          {venues.length === 0 ? (
            <Empty>No venue assertions in this release.</Empty>
          ) : (
            <ul className="space-y-1 text-sm">
              {venues.map((row) => (
                <li key={row.journal} className="flex justify-between gap-2">
                  <span className="truncate">{row.journal}</span>
                  <span className="text-xs text-neutral-500">{row.count}</span>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel title="Subject terms">
          <p className="mb-2 text-xs text-neutral-500">
            Subject assertions across sources. MeSH is one vocabulary among several, not a field
            of the domain.
          </p>
          {terms.length === 0 ? (
            <Empty>No subject assertions.</Empty>
          ) : (
            <ul className="flex flex-wrap gap-1 text-xs">
              {terms.map((row) => (
                <li key={row.term} className="rounded border border-neutral-200 px-2 py-0.5">
                  {row.term} <span className="text-neutral-400">{row.count}</span>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel title="Source contribution">
          <p className="mb-2 text-xs text-neutral-500">
            Unique contribution is what justifies keeping a connector, or retiring it.
          </p>
          {sources.length === 0 ? (
            <Empty>No source attribution yet.</Empty>
          ) : (
            <ul className="space-y-1 text-sm">
              {sources.map((row) => (
                <li key={row.connector_id} className="flex justify-between gap-2">
                  <span className="font-mono text-xs">{row.connector_id}</span>
                  <span className="text-xs text-neutral-500">
                    {row.works} works · {row.unique_contribution} unique
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>
    </div>
  );
}
