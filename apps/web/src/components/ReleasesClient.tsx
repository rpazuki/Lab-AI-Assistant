"use client";

import { useCallback, useEffect, useState } from "react";

import { Alert, Button, Empty, Panel, StateChip, row, rowList } from "@/components/ui/primitives";
import {
  approveRelease,
  buildExport,
  draftRelease,
  listReleases,
  publishRelease,
  rollbackRelease,
  validateRelease,
} from "@/lib/api";
import type { Release } from "@/types";

/**
 * Releases and exports.
 *
 * Gate results are shown before the publish button, and publishing is only offered
 * once every gate passes and the projection is ready. Rollback is one click and
 * changes an alias, not a row.
 */
export function ReleasesClient() {
  const [releases, setReleases] = useState<Release[]>([]);
  const [aliases, setAliases] = useState<
    { alias: string; release_id: string | null; previous_release_id: string | null }[]
  >([]);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const body = await listReleases();
      setReleases(body.items);
      setAliases(body.aliases);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "releases could not be loaded");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function run(label: string, action: () => Promise<unknown>) {
    setStatus(`${label}…`);
    try {
      const result = await action();
      setStatus(`${label}: ${JSON.stringify(result)}`);
      await refresh();
    } catch (caught) {
      setStatus(null);
      setError(caught instanceof Error ? caught.message : `${label} failed`);
    }
  }

  return (
    <div className="space-y-4">
      {error ? (
        <Alert tone="danger" role="alert">
          {error}
        </Alert>
      ) : null}
      {status ? (
        <Alert tone="info" className="break-all font-mono text-xs">
          {status}
        </Alert>
      ) : null}

      <Panel
        title="Serving aliases"
        aside={
          <Button
            variant="ghost"
            size="sm"
            onClick={() => run("rollback", () => rollbackRelease("current"))}
          >
            Roll back
          </Button>
        }
      >
        {aliases.length === 0 ? (
          <Empty>Nothing is published yet.</Empty>
        ) : (
          <ul className="space-y-1.5 text-sm">
            {aliases.map((alias) => (
              <li key={alias.alias} className="font-mono text-xs text-ink">
                <span className="font-semibold text-heading">{alias.alias}</span> →{" "}
                {alias.release_id ?? "unset"}
                {alias.previous_release_id ? (
                  <span className="text-muted"> (previous {alias.previous_release_id})</span>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <Panel
        title="Releases"
        flush
        aside={
          <Button
            variant="ghost"
            size="sm"
            onClick={() =>
              run("draft", () =>
                draftRelease({
                  name: "corpus",
                  semantic_version: new Date().toISOString().slice(0, 10).replaceAll("-", "."),
                }),
              )
            }
          >
            Draft from approved material
          </Button>
        }
      >
        {releases.length === 0 ? (
          <div className="px-5 py-3">
            <Empty>No releases yet.</Empty>
          </div>
        ) : null}
        <ul className={`${rowList} text-sm`}>
          {releases.map((release) => {
            const failed = Object.entries(release.gate_results).filter(([, ok]) => !ok);
            return (
              <li key={release.id} className={row}>
                <div className="flex flex-wrap items-center gap-2">
                  <strong className="text-heading">
                    {release.name}@{release.semantic_version}
                  </strong>
                  <StateChip state={release.status} />
                  <span className="ml-auto font-mono text-xs text-muted">
                    manifest {release.manifest_sha256?.slice(0, 12) ?? "—"}
                  </span>
                </div>
                {Object.keys(release.gate_results).length > 0 ? (
                  <ul className="mt-2 flex flex-wrap gap-1">
                    {Object.entries(release.gate_results).map(([gate, ok]) => (
                      <li key={gate}>
                        <StateChip state={`${gate}=${ok ? "pass" : "fail"}`} />
                      </li>
                    ))}
                  </ul>
                ) : null}
                <div className="mt-3 flex flex-wrap gap-2">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => run("validate", () => validateRelease(release.id))}
                  >
                    Validate
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={release.status !== "approved"}
                    onClick={() => run("approve", () => approveRelease(release.id))}
                  >
                    Approve and index
                  </Button>
                  <Button
                    size="sm"
                    disabled={
                      release.status !== "approved" ||
                      failed.length > 0 ||
                      release.projection_ready_profiles.length === 0
                    }
                    onClick={() => run("publish", () => publishRelease(release.id))}
                  >
                    Publish
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => run("export", () => buildExport(release.id))}
                  >
                    Build export
                  </Button>
                </div>
              </li>
            );
          })}
        </ul>
      </Panel>
    </div>
  );
}
