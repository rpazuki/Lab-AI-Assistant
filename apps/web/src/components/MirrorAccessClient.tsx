"use client";

/**
 * A mirror's own choice of what it serves.
 *
 * Everything on a mirror comes from its publisher: scopes, papers and releases.
 * This page is where the mirror's admin stops serving some of them, for everyone
 * on this mirror, admins included. Each list is a set of **Serve** boxes, and one
 * **Save** records the whole choice; until then nothing changes, so a slip of the
 * mouse removes nothing.
 *
 * The API decides everything here. The page only shows what it said and sends the
 * boxes back; on the publisher it says where the equivalent controls are instead.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { Alert, Button, Empty, Panel, StateChip } from "@/components/ui/primitives";
import { SortHeader } from "@/components/ui/table";
import { ApiError, getMirrorAccess, saveMirrorAccess } from "@/lib/api";
import { useTable } from "@/lib/useTable";
import type { MirrorAccess } from "@/types";

function when(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "";
}

function papers(count: number): string {
  return `${count} ${count === 1 ? "paper" : "papers"}`;
}

export function MirrorAccessClient() {
  const [view, setView] = useState<MirrorAccess | null>(null);
  const [notMirror, setNotMirror] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  // Ids of what is ticked to be served. Starts as what the server serves.
  const [scopes, setScopes] = useState<Set<string>>(new Set());
  const [releases, setReleases] = useState<Set<string>>(new Set());

  const adopt = useCallback((next: MirrorAccess) => {
    setView(next);
    setScopes(new Set(next.scopes.filter((s) => !s.withheld).map((s) => s.scope_id)));
    setReleases(new Set(next.releases.filter((r) => !r.withheld).map((r) => r.release_id)));
  }, []);

  useEffect(() => {
    getMirrorAccess()
      .then(adopt)
      .catch((cause: unknown) => {
        // 501: the operation lives on another kind of installation, here the mirror.
        if (cause instanceof ApiError && cause.status === 501) {
          setNotMirror(true);
        } else {
          setError(cause instanceof Error ? cause.message : String(cause));
        }
      });
  }, [adopt]);

  const scopeTable = useTable(view?.scopes ?? [], {
    columns: { scope: (row) => row.name.toLowerCase(), papers: (row) => row.papers },
    defaultSort: { key: "scope", direction: "asc" },
  });
  const releaseTable = useTable(view?.releases ?? [], {
    columns: {
      release: (row) => `${row.name} ${row.semantic_version}`.toLowerCase(),
      published: (row) => row.published_at ?? "",
    },
    defaultSort: { key: "published", direction: "desc" },
  });

  const changed = useMemo(() => {
    if (!view) return false;
    return (
      view.scopes.some((s) => s.withheld === scopes.has(s.scope_id)) ||
      view.releases.some((r) => r.withheld === releases.has(r.release_id))
    );
  }, [view, scopes, releases]);

  const toggle = (set: Set<string>, update: (next: Set<string>) => void, id: string) => {
    const next = new Set(set);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    update(next);
    setSaved(null);
  };

  async function save() {
    if (!view) return;
    setSaving(true);
    setError(null);
    try {
      const next = await saveMirrorAccess({
        withheld_scope_ids: view.scopes.filter((s) => !scopes.has(s.scope_id)).map((s) => s.scope_id),
        withheld_release_ids: view.releases
          .filter((r) => !releases.has(r.release_id))
          .map((r) => r.release_id),
      });
      adopt(next);
      setSaved("Saved. Ask, Search, Analytics and exports on this mirror follow it from now on.");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSaving(false);
    }
  }

  if (notMirror) {
    return (
      <Alert tone="info" role="status">
        This installation is the publisher, so it serves what it publishes. To keep a scope from
        some members, use its access rules under Collections; to stop serving a release, roll back or
        publish another on Releases. This page is for a mirror.
      </Alert>
    );
  }
  if (error && !view) return <Alert tone="danger" role="alert">{error}</Alert>;
  if (!view) return <Empty>Loading…</Empty>;

  const noneServed = view.releases.length > 0 && releases.size === 0;
  const fallback =
    !noneServed && view.releases.find((r) => r.publisher_choice && !releases.has(r.release_id));

  return (
    <div className="space-y-6">
      <Alert tone="info" role="status">
        Everything here comes from the publisher. Untick what this mirror should stop serving: it is
        left out of Ask, Search, Analytics and exports for everyone here, admins included. Imports
        never change this choice, and questions asked here stay on this mirror.
      </Alert>

      <Panel title="Scopes" flush>
        {view.scopes.length === 0 ? (
          <div className="p-5">
            <Empty>
              No scopes have arrived yet. They come with the next import of a snapshot the publisher
              made with this version.
            </Empty>
          </div>
        ) : (
          <table className="data-table" aria-label="Scopes">
            <thead>
              <tr>
                <th scope="col">Serve</th>
                <SortHeader table={scopeTable} column="scope">
                  Scope
                </SortHeader>
                <SortHeader table={scopeTable} column="papers" numeric>
                  Papers it found
                </SortHeader>
              </tr>
            </thead>
            <tbody>
              {scopeTable.rows.map((scope) => (
                <tr key={scope.scope_id}>
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`Serve ${scope.name}`}
                      checked={scopes.has(scope.scope_id)}
                      onChange={() => toggle(scopes, setScopes, scope.scope_id)}
                    />
                  </td>
                  <td>{scope.name}</td>
                  <td className="numeric">{scope.papers}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
      <p className="-mt-4 text-sm text-muted">
        A paper is left out only when every scope that found it is unticked. Now left out:{" "}
        {papers(view.withheld_papers)}.
      </p>

      <Panel title="Releases" flush>
        {view.releases.length === 0 ? (
          <div className="p-5">
            <Empty>No published release has been imported yet.</Empty>
          </div>
        ) : (
          <table className="data-table" aria-label="Releases">
            <thead>
              <tr>
                <th scope="col">Serve</th>
                <SortHeader table={releaseTable} column="release">
                  Release
                </SortHeader>
                <SortHeader table={releaseTable} column="published">
                  Published
                </SortHeader>
                <th scope="col">State</th>
                <th scope="col">On this mirror</th>
              </tr>
            </thead>
            <tbody>
              {releaseTable.rows.map((release) => (
                <tr key={release.release_id}>
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`Serve ${release.name} ${release.semantic_version}`}
                      checked={releases.has(release.release_id)}
                      onChange={() => toggle(releases, setReleases, release.release_id)}
                    />
                  </td>
                  <td>
                    {release.name} <span className="text-muted">{release.semantic_version}</span>
                  </td>
                  <td>{when(release.published_at)}</td>
                  <td>
                    <StateChip state={release.status} kind="release" />
                  </td>
                  <td>
                    {[
                      release.serving ? "Answering from it now" : null,
                      release.publisher_choice ? "The publisher's choice" : null,
                    ]
                      .filter(Boolean)
                      .join(". ")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
      <p className="-mt-4 text-sm text-muted">
        This mirror answers from the publisher&apos;s choice of release, unless it is unticked; then
        from the newest ticked one.
      </p>

      {noneServed ? (
        <Alert tone="warning" role="status">
          With no release ticked, this mirror serves nothing: Ask and Search will say nothing is
          published.
        </Alert>
      ) : fallback ? (
        <Alert tone="warning" role="status">
          {`${fallback.name} ${fallback.semantic_version}`} is the publisher&apos;s choice. Unticked,
          this mirror answers from the newest ticked release instead.
        </Alert>
      ) : null}

      <div className="flex flex-wrap items-center gap-3">
        <Button onClick={save} disabled={!changed || saving}>
          {saving ? "Saving…" : "Save"}
        </Button>
        {changed ? (
          <Button
            variant="ghost"
            onClick={() => {
              adopt(view);
              setSaved(null);
            }}
            disabled={saving}
          >
            Undo changes
          </Button>
        ) : null}
        {view.set_at && !changed ? (
          <span className="text-sm text-muted">Last changed {when(view.set_at)}.</span>
        ) : null}
      </div>
      {saved ? <Alert tone="success" role="status">{saved}</Alert> : null}
      {error ? <Alert tone="danger" role="alert">{error}</Alert> : null}
    </div>
  );
}
