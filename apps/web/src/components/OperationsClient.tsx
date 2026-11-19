"use client";

import { useCallback, useEffect, useState } from "react";

import { Alert, Button, Empty, Panel, StateChip } from "@/components/ui/primitives";
import { listAuditEvents, retryTask, taskSnapshot } from "@/lib/api";
import type { AuditEvent, TaskSnapshot } from "@/types";

/** The task states worth a tile, in the order work moves through them. */
const STATES = [
  "pending",
  "ready",
  "leased",
  "blocked",
  "awaiting_user",
  "degraded",
  "quarantined",
  "failed",
  "dead_letter",
  "succeeded",
] as const;

const REFRESH_MS = 15_000;

function ago(iso: string): string {
  const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60_000));
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.round(minutes / 60);
  return hours < 48 ? `${hours} h` : `${Math.round(hours / 24)} d`;
}

function summarise(metadata: Record<string, unknown>): string {
  return Object.entries(metadata)
    .map(([key, value]) => `${key}=${typeof value === "object" ? JSON.stringify(value) : String(value)}`)
    .join(" · ");
}

/**
 * Operations: the durable task queue and the audit trail.
 *
 * Everything here is read from task rows and audit events, never inferred from a
 * worker heartbeat: a task is finished when its row says so. The page refreshes
 * itself every 15 seconds; retrying a dead-lettered task puts it back in the
 * queue, where the normal lease and retry policy applies again.
 */
export function OperationsClient() {
  const [snapshot, setSnapshot] = useState<TaskSnapshot | null>(null);
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [nextSnapshot, nextEvents] = await Promise.all([taskSnapshot(), listAuditEvents(100)]);
      setSnapshot(nextSnapshot);
      setEvents(nextEvents);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "operations could not be loaded");
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  async function retry(taskId: string) {
    setBusy(taskId);
    setNotice(null);
    let report: { ok: boolean; message: string };
    try {
      const result = await retryTask(taskId);
      report = { ok: true, message: `Task ${taskId.slice(0, 8)} is back in the queue as ${result.state}.` };
    } catch (caught) {
      report = { ok: false, message: caught instanceof Error ? caught.message : "the task was not retried" };
    } finally {
      setBusy(null);
    }
    // Reported after the reload, which clears the error banner when it succeeds.
    await refresh();
    if (report.ok) setNotice(report.message);
    else setError(report.message);
  }

  const oldest = Object.entries(snapshot?.oldest_pending_by_stage ?? {});

  return (
    <div className="space-y-4">
      {error ? (
        <Alert tone="danger" role="alert">
          {error}
        </Alert>
      ) : null}
      {notice ? (
        <Alert tone="success" role="status">
          {notice}
        </Alert>
      ) : null}

      <section aria-label="Tasks by state">
        <dl className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          {STATES.map((state) => (
            <div
              key={state}
              className="rounded-card border border-border bg-surface px-4 py-3 shadow-card"
            >
              <dt className="font-mono text-xs text-muted">{state}</dt>
              <dd className="mt-1 text-stat font-semibold text-heading tabular-nums">
                {snapshot ? (snapshot.counts[state] ?? 0) : "…"}
              </dd>
            </div>
          ))}
        </dl>
      </section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="Waiting longest, by stage" flush>
          {oldest.length === 0 ? (
            <div className="px-5 py-3">
              <Empty>Nothing is waiting to run.</Empty>
            </div>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Stage</th>
                  <th className="numeric">Oldest waiting</th>
                </tr>
              </thead>
              <tbody>
                {oldest.map(([stage, since]) => (
                  <tr key={stage}>
                    <td className="font-mono text-xs">{stage}</td>
                    <td className="numeric" title={since}>
                      {ago(since)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>

        <Panel title="Waiting for a person" flush>
          {(snapshot?.awaiting_user ?? []).length === 0 ? (
            <div className="px-5 py-3">
              <Empty>No task needs a decision.</Empty>
            </div>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Task</th>
                  <th>Stage</th>
                  <th>Needs</th>
                </tr>
              </thead>
              <tbody>
                {snapshot?.awaiting_user.map((task) => (
                  <tr key={task.task_id}>
                    <td className="font-mono text-xs">{task.task_id.slice(0, 8)}</td>
                    <td className="font-mono text-xs">{task.stage}</td>
                    <td>{task.action ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      </div>

      <Panel title="Dead letters" flush>
        {(snapshot?.dead_letters ?? []).length === 0 ? (
          <div className="px-5 py-3">
            <Empty>No task has exhausted its retries.</Empty>
          </div>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>Task</th>
                <th>Stage</th>
                <th>Last error</th>
                <th>
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {snapshot?.dead_letters.map((task) => (
                <tr key={task.task_id}>
                  <td className="font-mono text-xs">{task.task_id.slice(0, 8)}</td>
                  <td className="font-mono text-xs">{task.stage}</td>
                  <td className="max-w-md break-words text-xs text-danger-strong">
                    {task.error ?? "—"}
                  </td>
                  <td>
                    <div className="flex justify-end">
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={busy === task.task_id}
                        onClick={() => retry(task.task_id)}
                      >
                        Retry
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      <Panel title="Audit log (latest 100)" flush>
        {events.length === 0 ? (
          <div className="px-5 py-3">
            <Empty>No audit events yet.</Empty>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Action</th>
                  <th>Subject</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {events.map((event) => (
                  <tr key={event.id}>
                    <td className="whitespace-nowrap text-xs text-muted" title={event.occurred_at}>
                      {new Date(event.occurred_at).toLocaleString()}
                    </td>
                    <td>
                      <StateChip state={event.action} />
                    </td>
                    <td className="font-mono text-xs">
                      {event.subject_type}
                      {event.subject_id ? `:${event.subject_id.slice(0, 8)}` : ""}
                    </td>
                    <td className="max-w-lg break-words text-xs text-muted">
                      {[event.reason_code, summarise(event.metadata)].filter(Boolean).join(" · ") ||
                        "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
