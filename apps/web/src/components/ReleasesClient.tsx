"use client";

import { useCallback, useState } from "react";

import { DeleteButton } from "@/components/ui/DeleteButton";
import { LiveStatus } from "@/components/ui/LiveStatus";
import { Alert, Button, Empty, Panel, StateChip, row, rowList } from "@/components/ui/primitives";
import {
  ApiError,
  approveRelease,
  buildExport,
  deleteRelease,
  exportDownloadUrl,
  draftRelease,
  listReleaseGates,
  listReleases,
  publishRelease,
  rollbackRelease,
  validateRelease,
} from "@/lib/api";
import { useLiveRefresh } from "@/lib/useLiveRefresh";
import type { GateStatus, Release } from "@/types";

/** A release the worker is still validating, indexing or publishing. */
function inProgress(release: Release): boolean {
  return (
    release.status === "validating" ||
    release.status === "publishing" ||
    (release.status === "approved" && release.projection_ready_profiles.length === 0)
  );
}

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
  const [gateStatus, setGateStatus] = useState<Record<string, GateStatus[]>>({});
  const [refused, setRefused] = useState<{ releaseId: string; blockers: string[] } | null>(null);
  const [exported, setExported] = useState<{
    releaseId: string;
    valid: boolean;
    problems: string[];
    files: string[];
  } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const body = await listReleases();
      setReleases(body.items);
      setAliases(body.aliases);
      // Required benchmarks, for the releases that could be published next.
      const candidates = body.items.filter((release) => release.status === "approved");
      const statuses = await Promise.all(
        candidates.map((release) =>
          listReleaseGates(release.id)
            .then((answer) => [release.id, answer.status ?? []] as const)
            .catch(() => [release.id, [] as GateStatus[]] as const),
        ),
      );
      setGateStatus(Object.fromEntries(statuses));
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "releases could not be loaded");
    }
  }, []);

  const live = useLiveRefresh(refresh, { active: releases.some(inProgress) });

  /**
   * Publish, and when the refusal is a required benchmark rather than the release
   * itself, say which ones and offer an admin's override with a reason.
   */
  async function publish(release: Release, overrideReason?: string) {
    setRefused(null);
    setStatus("publishing…");
    try {
      await publishRelease(release.id, "current", overrideReason);
      setStatus(`${release.name}@${release.semantic_version} is now current.`);
      setError(null);
      live.kick();
      await live.refresh();
    } catch (caught) {
      setStatus(null);
      const blockers =
        caught instanceof ApiError && Array.isArray(caught.context.blockers)
          ? (caught.context.blockers as unknown[]).map(String)
          : [];
      if (blockers.length > 0 && blockers.every((item) => item.startsWith("evaluation:"))) {
        setRefused({ releaseId: release.id, blockers });
        setError(null);
      } else {
        setError(caught instanceof Error ? caught.message : "publish failed");
      }
    }
  }

  async function run(label: string, action: () => Promise<unknown>) {
    setStatus(`${label}…`);
    try {
      const result = await action();
      setStatus(`${label}: ${JSON.stringify(result)}`);
      live.kick();
      await live.refresh();
    } catch (caught) {
      setStatus(null);
      setError(caught instanceof Error ? caught.message : `${label} failed`);
    }
  }

  async function remove(release: Release) {
    setStatus("deleting…");
    try {
      await deleteRelease(release.id);
      setStatus(`${release.name}@${release.semantic_version} deleted.`);
      setError(null);
    } catch (caught) {
      setStatus(null);
      setError(caught instanceof Error ? caught.message : "the release was not deleted");
    }
    await live.refresh();
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
          <span className="flex flex-wrap items-center gap-3">
            <LiveStatus {...live} />
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
          </span>
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
                  <StateChip state={release.status} kind="release" />
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
                    onClick={() => publish(release)}
                  >
                    Publish
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={async () => {
                      setStatus("Building the export…");
                      try {
                        const built = await buildExport(release.id);
                        setExported({ releaseId: release.id, ...built });
                        setStatus(null);
                        setError(null);
                      } catch (caught) {
                        setStatus(null);
                        setError(caught instanceof Error ? caught.message : "the export failed");
                      }
                    }}
                  >
                    Build export
                  </Button>
                  {release.status === "draft" || release.status === "rejected" ? (
                    <DeleteButton
                      what={`release ${release.name}@${release.semantic_version}`}
                      consequence="It was never approved, so nothing was served from it; it is refused while another release, an evaluation run or a conversation points at it."
                      onDelete={() => void remove(release)}
                    />
                  ) : null}
                </div>
                {(gateStatus[release.id] ?? []).length > 0 ? (
                  <ul aria-label="Required benchmarks" className="mt-2 flex flex-wrap gap-1">
                    {(gateStatus[release.id] ?? []).map((gate) => (
                      <li key={gate.key}>
                        <StateChip state={`${gate.key}=${gate.passed ? "pass" : gate.run_status ?? "not run"}`} />
                      </li>
                    ))}
                  </ul>
                ) : null}
                {refused?.releaseId === release.id ? (
                  <div className="mt-3 space-y-2">
                    <Alert tone="warning" role="alert">
                      Not published: a required benchmark has not passed on this release (
                      {refused.blockers.map((item) => item.replace(/^evaluation:| has not passed$/g, "")).join(", ")}
                      ). Run it on the Evaluation page, or an admin may publish anyway.
                    </Alert>
                    <Button
                      variant="danger"
                      size="sm"
                      onClick={() => {
                        const reason = window.prompt(
                          "Why publish without a passing run? The reason is recorded in the audit log.",
                        );
                        if (reason && reason.trim()) void publish(release, reason.trim());
                      }}
                    >
                      Publish anyway (admin)
                    </Button>
                  </div>
                ) : null}
                {exported?.releaseId === release.id ? (
                  <ExportFiles exported={exported} />
                ) : null}
              </li>
            );
          })}
        </ul>
      </Panel>
    </div>
  );
}

/** The files of a built export, each a download, with the whole package as a zip. */
export function ExportFiles({
  exported,
}: {
  exported: { releaseId: string; valid: boolean; problems: string[]; files: string[] };
}) {
  const data = exported.files.filter((file) => file.startsWith("data/"));
  const other = exported.files.filter((file) => !file.startsWith("data/"));
  return (
    <div className="mt-3 space-y-2 rounded-control border border-border bg-surface-subtle px-4 py-3 text-sm">
      {exported.valid ? null : (
        <Alert tone="warning" role="status">
          The package did not validate: {exported.problems.join("; ")}
        </Alert>
      )}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
        <a
          className="font-semibold text-link underline hover:no-underline"
          href={exportDownloadUrl(exported.releaseId)}
          download
        >
          Download everything (.zip)
        </a>
        {data.map((file) => (
          <a
            key={file}
            className="font-mono text-xs text-link underline hover:no-underline"
            href={exportDownloadUrl(exported.releaseId, file)}
            download
          >
            {file.replace(/^data\//, "")}
          </a>
        ))}
      </div>
      {other.length > 0 ? (
        <p className="text-xs text-muted">
          Also in the zip: {other.join(", ")}
        </p>
      ) : null}
    </div>
  );
}
