"use client";

import { useEffect, useState } from "react";

import { Alert, Empty, Panel, StateChip } from "@/components/ui/primitives";
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
      <Alert tone="danger" role="alert">
        {error}
      </Alert>
    );
  }
  if (!stats) return <Empty>Loading…</Empty>;

  const tiles: [string, number | null][] = [
    ["Works", stats.works],
    ["Renditions", stats.renditions],
    ["Claims", stats.claims],
    ["Chunks", stats.chunks],
    // Stewards see how much is withheld; everyone else sees that some of the
    // release is withheld, which is what they need to ask for access.
    ["Withheld by policy", stats.withheld_by_policy ?? null],
  ];
  const peak = Math.max(1, ...years.map((row) => row.count));

  return (
    <div className="space-y-6">
      <section aria-labelledby="release-heading" className="space-y-3">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 id="release-heading" className="text-card-title font-semibold text-heading">
            Release{" "}
            <span className="font-mono">
              {stats.release_name}@{stats.release_version}
            </span>
          </h2>
          <p className="text-xs text-muted">
            Published {stats.published_at ?? "not yet"}. Counts follow the same access policy as
            retrieval.
          </p>
        </div>
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-5">
          {tiles.map(([label, value]) => (
            <div
              key={label}
              className="rounded-card border border-border bg-surface px-5 py-4 shadow-card"
            >
              <dt className="text-pretitle font-semibold uppercase tracking-wider text-muted">
                {label}
              </dt>
              <dd className="mt-1 text-stat font-semibold text-heading tabular-nums">
                {value === null ? <StateChip state="restricted" kind="flag" /> : value}
              </dd>
            </div>
          ))}
        </dl>
      </section>

      <div className="grid gap-6 md:grid-cols-2">
        <Panel title="Publication years">
          {years.length === 0 ? (
            <Empty>No dated works in this release.</Empty>
          ) : (
            <ul className="space-y-1.5 text-sm">
              {years.map((row) => (
                <li key={row.year} className="flex items-center gap-3">
                  <span className="w-12 font-mono text-xs text-muted">{row.year}</span>
                  <span className="h-2.5 flex-1 overflow-hidden rounded-full bg-clouds" aria-hidden>
                    <span
                      className="block h-full rounded-full bg-accent"
                      style={{ width: `${(row.count / peak) * 100}%` }}
                    />
                  </span>
                  <span className="w-8 text-right text-xs tabular-nums text-ink">{row.count}</span>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel title="Venues" flush>
          {venues.length === 0 ? (
            <div className="px-5 py-3">
              <Empty>No venue assertions in this release.</Empty>
            </div>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Venue</th>
                  <th scope="col" className="numeric">
                    Works
                  </th>
                </tr>
              </thead>
              <tbody>
                {venues.map((row) => (
                  <tr key={row.journal}>
                    <td className="max-w-0 truncate">{row.journal}</td>
                    <td className="numeric">{row.count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>

        <Panel title="Subject terms">
          <p className="mb-3 text-xs text-muted">
            Subject assertions across sources. MeSH is one vocabulary among several, not a field
            of the domain.
          </p>
          {terms.length === 0 ? (
            <Empty>No subject assertions.</Empty>
          ) : (
            <ul className="flex flex-wrap gap-1.5 text-xs">
              {terms.map((row) => (
                <li
                  key={row.term}
                  className="rounded-badge border border-border bg-surface-subtle px-2 py-0.5 text-ink"
                >
                  {row.term} <span className="tabular-nums text-muted">{row.count}</span>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel title="Source contribution" flush>
          <p className="px-5 pt-4 pb-3 text-xs text-muted">
            Unique contribution is what justifies keeping a connector, or retiring it.
          </p>
          {sources.length === 0 ? (
            <div className="px-5 pb-3">
              <Empty>No source attribution yet.</Empty>
            </div>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Connector</th>
                  <th scope="col" className="numeric">
                    Works
                  </th>
                  <th scope="col" className="numeric">
                    Unique
                  </th>
                </tr>
              </thead>
              <tbody>
                {sources.map((row) => (
                  <tr key={row.connector_id}>
                    <td className="font-mono text-xs">{row.connector_id}</td>
                    <td className="numeric">{row.works}</td>
                    <td className="numeric">{row.unique_contribution}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      </div>
    </div>
  );
}
