"use client";

import { useCallback, useState } from "react";

import { LiveStatus } from "@/components/ui/LiveStatus";
import { Alert, Empty, Panel } from "@/components/ui/primitives";
import { SortHeader } from "@/components/ui/table";
import { collectionProgress } from "@/lib/api";
import { describeStatus } from "@/lib/statuses";
import { useLiveRefresh } from "@/lib/useLiveRefresh";
import { useTable } from "@/lib/useTable";
import { utcWhen } from "@/lib/when";
import type { CollectionProgress, SiteRow } from "@/types";

/** Why a paper is not fetched, in the words a steward acts on. */
const NOT_FETCHED: Record<string, string> = {
  none: "No open licence: only an upload is allowed",
  authentication_required: "The publisher asks for a sign-in",
  entitlement_required: "Needs a subscription",
  invalid_content: "The link gave a web page, not a PDF",
  rate_limited_retryable: "The source was rate-limiting (tried again on the next fetch press)",
  source_unavailable_retryable: "The source was down (tried again on the next fetch press)",
  not_found: "No open copy found",
  robots_or_policy_blocked: "The site forbids automated downloads",
  licence_not_permitted: "The licence does not allow keeping a copy",
  identity_mismatch: "The file could not be tied to the paper",
  failed_permanent: "A permanent error from the source",
  too_large: "The file is too large",
  metadata_only: "The source has the record but no full text",
};

const RELEVANCE: Record<string, string> = {
  included: "Included",
  included_uncertain: "Included, uncertain",
  unreviewed: "Left for review",
  excluded: "Excluded",
};

const IDENTITY: Record<string, string> = {
  identifier: "Its DOI, PMID or PMCID is in the file",
  title: "Its title is in the file",
  unconfirmed: "Not tied to its paper yet",
};

const n = (value: number | undefined) => (value ?? 0).toLocaleString();
const share = (part: number, whole: number) =>
  whole > 0 ? `${Math.round((part / whole) * 1000) / 10}%` : "—";

/** A labelled bar per row, scaled to the largest, with its count. */
function Bars({
  rows,
  label,
  empty,
}: {
  rows: { key: string; label: string; count: number; title?: string }[];
  label: string;
  empty: string;
}) {
  const peak = Math.max(1, ...rows.map((row) => row.count));
  if (rows.length === 0) return <Empty>{empty}</Empty>;
  return (
    <ul aria-label={label} className="space-y-1.5 text-sm">
      {rows.map((row) => (
        <li key={row.key} className="flex items-center gap-3" title={row.title}>
          <span className="w-64 shrink-0 text-xs text-ink">{row.label}</span>
          <span className="h-2.5 flex-1 overflow-hidden rounded-full bg-clouds" aria-hidden>
            <span
              className="block h-full rounded-full bg-accent"
              style={{ width: `${(row.count / peak) * 100}%` }}
            />
          </span>
          <span className="w-16 text-right text-xs tabular-nums text-ink">{n(row.count)}</span>
        </li>
      ))}
    </ul>
  );
}

function Figures({ items }: { items: { label: string; value: string; note?: string }[] }) {
  return (
    <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3">
      {items.map((item) => (
        <div key={item.label} className="rounded-control border border-border px-3 py-2">
          <dt className="text-xs text-muted">{item.label}</dt>
          <dd className="text-lg font-semibold tabular-nums text-heading">{item.value}</dd>
          {item.note ? <dd className="text-xs text-muted">{item.note}</dd> : null}
        </div>
      ))}
    </dl>
  );
}

/** Each publisher site: what it gave, what it refused, and whether it is resting. */
function Sites({ sites }: { sites: CollectionProgress["sites"] }) {
  const table = useTable(sites.rows, {
    columns: {
      host: (row: SiteRow) => row.host,
      fetched: (row: SiteRow) => row.fetched,
      slowed_down: (row: SiteRow) => row.slowed_down,
      blocked: (row: SiteRow) => row.blocked,
      sign_in: (row: SiteRow) => row.sign_in,
      other: (row: SiteRow) => row.other,
      resting_until: (row: SiteRow) => row.resting_until ?? "",
    },
  });
  if (sites.rows.length === 0) {
    return <Empty>No site has been asked by the current fetcher yet.</Empty>;
  }
  return (
    <div className="space-y-2">
      <p className="text-xs text-muted">
        Each site is asked once every {sites.seconds_between_requests} s at most, one request at a
        time. A site that slows us down or blocks rests, longer each time in a row, while other
        sites are fetched; its papers wait for it without using up their tries.{" "}
        {sites.resting > 0 ? `${n(sites.resting)} resting now. ` : ""}
        {sites.total > sites.rows.length
          ? `The ${sites.rows.length} that refused most of ${n(sites.total)} sites.`
          : ""}
      </p>
      <div className="overflow-x-auto rounded-control border border-border">
        <table className="data-table" aria-label="Publisher sites">
          <thead>
            <tr>
              <SortHeader table={table} column="host">
                Site
              </SortHeader>
              <SortHeader table={table} column="fetched" numeric>
                Fetched
              </SortHeader>
              <SortHeader table={table} column="slowed_down" numeric>
                Slowed us down
              </SortHeader>
              <SortHeader table={table} column="blocked" numeric>
                Blocked
              </SortHeader>
              <SortHeader table={table} column="sign_in" numeric>
                Sign-in page
              </SortHeader>
              <SortHeader table={table} column="other" numeric>
                Other
              </SortHeader>
              <SortHeader table={table} column="resting_until">
                Resting until
              </SortHeader>
            </tr>
          </thead>
          <tbody>
            {table.rows.map((row) => (
              <tr key={row.host}>
                <td className="font-mono text-xs">{row.host}</td>
                <td className="numeric">{n(row.fetched)}</td>
                <td className="numeric">{n(row.slowed_down)}</td>
                <td className="numeric">{n(row.blocked)}</td>
                <td className="numeric">{n(row.sign_in)}</td>
                <td className="numeric">{n(row.other)}</td>
                <td className="text-xs">
                  {row.resting_until
                    ? `${utcWhen(row.resting_until)} (refused ${row.strikes} in a row)`
                    : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const fromCounts = (counts: Record<string, number>, labels: Record<string, string> = {}) =>
  Object.entries(counts)
    .filter(([, count]) => count > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([key, count]) => ({ key, label: labels[key] ?? key.replace(/_/g, " "), count }));

/**
 * Collection progress: the collection before publication, stage by stage.
 *
 * Analytics describes the release Ask serves; this describes the work behind it.
 * The funnel shows how far papers have got, and each stage says why the rest
 * have not: what the sources answered, why fetches failed, why parses were
 * degraded. Every count is over the whole workspace, recounted on each refresh.
 */
export function ProgressClient() {
  const [view, setView] = useState<CollectionProgress | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setView(await collectionProgress());
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "progress could not be loaded");
    }
  }, []);
  const live = useLiveRefresh(refresh, { active: false, idleMs: 60_000 });

  if (error) {
    return (
      <Alert tone="danger" role="alert">
        {error}
      </Alert>
    );
  }
  if (!view) return <p className="text-sm text-muted">Loading…</p>;

  const found = view.funnel[0]?.count ?? 0;
  const { screening, discovery, acquisition, parsing, extraction } = view;
  const daily = view.daily;
  const peakDay = Math.max(1, ...daily.map((day) => Math.max(day.found, day.fetched, day.parsed)));

  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <LiveStatus {...live} />
      </div>

      <Panel title="How far papers have got">
        <ol aria-label="Funnel" className="space-y-1.5 text-sm">
          {view.funnel.map((row) => (
            <li key={row.stage} className="flex items-center gap-3">
              <span className="w-48 shrink-0 text-xs text-ink">{row.label}</span>
              <span className="h-3 flex-1 overflow-hidden rounded-full bg-clouds" aria-hidden>
                <span
                  className="block h-full rounded-full bg-accent"
                  style={{ width: `${found ? (row.count / found) * 100 : 0}%` }}
                />
              </span>
              <span className="w-20 text-right text-xs tabular-nums text-ink">{n(row.count)}</span>
              <span className="w-14 text-right text-xs tabular-nums text-muted">{share(row.count, found)}</span>
            </li>
          ))}
        </ol>
      </Panel>

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="Discovery">
          <div className="space-y-4">
            <Figures
              items={[
                { label: "Discovery runs", value: n(discovery.runs) },
                { label: "Merged into another paper", value: n(discovery.merged) },
                { label: "Duplicate pairs to review", value: n(discovery.open_duplicate_pairs) },
                { label: "Identity conflicts", value: n(discovery.identity_conflicts) },
              ]}
            />
            <Bars
              label="Papers by source"
              empty="No source has found anything yet."
              rows={fromCounts(discovery.by_source)}
            />
          </div>
        </Panel>

        <Panel title="Screening">
          <div className="space-y-4">
            <Figures
              items={[
                {
                  label: "With an abstract",
                  value: share(screening.with_abstract, screening.found),
                  note: `${n(screening.with_abstract)} of ${n(screening.found)}`,
                },
                {
                  label: "Included with an abstract",
                  value: share(screening.included_with_abstract, screening.included),
                  note: `${n(screening.included_with_abstract)} of ${n(screening.included)}`,
                },
                { label: "Decided by a person", value: n(screening.decided_by_person) },
              ]}
            />
            <Bars label="Papers by relevance" empty="Nothing screened yet." rows={fromCounts(screening.relevance, RELEVANCE)} />
          </div>
        </Panel>

        <Panel title="Fetching full text">
          <div className="space-y-4">
            <Figures
              items={[
                { label: "Fetched", value: n(acquisition.plans.acquired) },
                { label: "Waiting for a person", value: n(acquisition.plans.assisted) },
                {
                  label: "Abstract only",
                  value: n(acquisition.plans.partial),
                  note: "its abstract stands in until an upload",
                },
                { label: "Failed", value: n(acquisition.plans.failed) },
                { label: "Still being fetched", value: n(acquisition.plans.planned) },
                { label: "Preprint versions", value: n(acquisition.preprints) },
              ]}
            />
            <div>
              <h3 className="mb-1.5 text-xs font-semibold text-heading">Why papers wait for a person</h3>
              <Bars
                label="Why papers wait for a person"
                empty="No paper is waiting."
                rows={fromCounts(acquisition.waiting_reasons, NOT_FETCHED).map((row) => ({
                  ...row,
                  title: describeStatus("acquisition_outcome", row.key) ?? undefined,
                }))}
              />
            </div>
            {Object.keys(acquisition.failed_reasons).length > 0 ? (
              <div>
                <h3 className="mb-1.5 text-xs font-semibold text-heading">Why fetches failed</h3>
                <Bars label="Why fetches failed" empty="" rows={fromCounts(acquisition.failed_reasons, NOT_FETCHED)} />
              </div>
            ) : null}
            <div>
              <h3 className="mb-1.5 text-xs font-semibold text-heading">How fetched files were tied to their paper</h3>
              <Bars label="How fetched files were tied to their paper" empty="Nothing fetched yet." rows={fromCounts(acquisition.identity, IDENTITY)} />
            </div>
          </div>
        </Panel>

        <Panel title="Parsing and extraction">
          <div className="space-y-4">
            <Figures
              items={[
                { label: "Parsed, ready", value: n(parsing.by_status.ready) },
                { label: "Parsed, degraded", value: n(parsing.by_status.degraded) },
                { label: "Papers with values", value: n(extraction.papers_with_values) },
                { label: "Values extracted", value: n(extraction.values) },
                { label: "Values waiting for review", value: n(extraction.waiting_review) },
              ]}
            />
            <div>
              <h3 className="mb-1.5 text-xs font-semibold text-heading">Why documents are degraded</h3>
              <Bars label="Why documents are degraded" empty="No document is degraded." rows={fromCounts(parsing.degraded_by_reason)} />
            </div>
          </div>
        </Panel>
      </div>

      <Panel title="Publisher sites">
        <Sites sites={view.sites} />
      </Panel>

      <Panel title={`The last ${daily.length} days (UTC)`}>
        <ol aria-label="Daily" className="space-y-1 text-xs">
          {daily.map((day) => (
            <li key={day.day} className="grid grid-cols-[6rem_1fr_1fr_1fr] items-center gap-3">
              <span className="font-mono text-muted">{day.day}</span>
              {(["found", "fetched", "parsed"] as const).map((kind) => (
                <span key={kind} className="flex items-center gap-2" title={`${kind} on ${day.day}`}>
                  <span className="h-2 flex-1 overflow-hidden rounded-full bg-clouds" aria-hidden>
                    <span
                      className="block h-full rounded-full bg-accent"
                      style={{ width: `${(day[kind] / peakDay) * 100}%` }}
                    />
                  </span>
                  <span className="w-14 text-right tabular-nums text-ink">{n(day[kind])}</span>
                </span>
              ))}
            </li>
          ))}
        </ol>
        <p className="mt-2 grid grid-cols-[6rem_1fr_1fr_1fr] gap-3 text-xs text-muted">
          <span />
          <span>Papers found</span>
          <span>Files fetched</span>
          <span>Documents parsed</span>
        </p>
      </Panel>
    </div>
  );
}
