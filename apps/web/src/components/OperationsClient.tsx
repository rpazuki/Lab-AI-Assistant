"use client";

import { useCallback, useState } from "react";

import { InfoTip } from "@/components/ui/InfoTip";
import { LiveStatus } from "@/components/ui/LiveStatus";
import { Alert, Button, Empty, Panel, StateChip } from "@/components/ui/primitives";
import { SortHeader, TableSearch } from "@/components/ui/table";
import { listAuditEvents, retryTask, taskSnapshot } from "@/lib/api";
import { describeStatus } from "@/lib/statuses";
import { useLiveRefresh } from "@/lib/useLiveRefresh";
import { useTable } from "@/lib/useTable";
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
/** The most the API returns; the search box narrows these, newest first. */
const AUDIT_LIMIT = 500;

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
      const [nextSnapshot, nextEvents] = await Promise.all([taskSnapshot(), listAuditEvents(AUDIT_LIMIT)]);
      setSnapshot(nextSnapshot);
      setEvents(nextEvents);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "operations could not be loaded");
    }
  }, []);

  // The queue is always worth watching here; faster while a worker holds a task.
  const live = useLiveRefresh(refresh, {
    active: (snapshot?.counts.leased ?? 0) > 0,
    idleMs: REFRESH_MS,
  });

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
    live.kick();
    await live.refresh();
    if (report.ok) setNotice(report.message);
    else setError(report.message);
  }

  const oldest = useTable(Object.entries(snapshot?.oldest_pending_by_stage ?? {}), {
    columns: { stage: ([stage]) => stage, since: ([, since]) => since },
    // The panel is about what has waited longest, so its oldest entry leads.
    defaultSort: { key: "since", direction: "asc" },
  });
  const awaiting = useTable(snapshot?.awaiting_user ?? [], {
    columns: { task: (task) => task.task_id, stage: (task) => task.stage, needs: (task) => task.action },
    searchText: (task) => `${task.task_id} ${task.stage} ${task.action ?? ""}`,
  });
  const dead = useTable(snapshot?.dead_letters ?? [], {
    columns: { task: (task) => task.task_id, stage: (task) => task.stage, error: (task) => task.error },
    searchText: (task) => `${task.task_id} ${task.stage} ${task.error ?? ""}`,
  });
  const audit = useTable(events, {
    columns: {
      when: (event) => event.occurred_at,
      action: (event) => event.action,
      subject: (event) => `${event.subject_type}:${event.subject_id ?? ""}`,
      detail: (event) => event.reason_code,
    },
    defaultSort: { key: "when", direction: "desc" },
    searchText: (event) =>
      [
        event.action,
        describeStatus("audit_action", event.action) ?? "",
        event.actor_type,
        event.subject_type,
        event.subject_id ?? "",
        event.reason_code ?? "",
        summarise(event.metadata),
      ].join(" "),
  });

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

      <div className="flex justify-end">
        <LiveStatus {...live} />
      </div>

      <section aria-label="Tasks by state">
        <dl className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          {STATES.map((state) => (
            <div
              key={state}
              className="rounded-card border border-border bg-surface px-4 py-3 shadow-card"
            >
              <dt className="flex items-center gap-1 font-mono text-xs text-muted">
                {state}
                <InfoTip label={state}>{describeStatus("task", state)}</InfoTip>
              </dt>
              <dd className="mt-1 text-stat font-semibold text-heading tabular-nums">
                {snapshot ? (snapshot.counts[state] ?? 0) : "…"}
              </dd>
            </div>
          ))}
        </dl>
      </section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="Waiting longest, by stage" flush>
          {oldest.total === 0 ? (
            <div className="px-5 py-3">
              <Empty>Nothing is waiting to run.</Empty>
            </div>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <SortHeader table={oldest} column="stage">
                    Stage
                  </SortHeader>
                  <SortHeader table={oldest} column="since" numeric>
                    Oldest waiting
                  </SortHeader>
                </tr>
              </thead>
              <tbody>
                {oldest.rows.map(([stage, since]) => (
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
          {awaiting.total === 0 ? (
            <div className="px-5 py-3">
              <Empty>No task needs a decision.</Empty>
            </div>
          ) : (
            <>
            <TableSearch table={awaiting} label="tasks waiting for a person" />
            <table className="data-table">
              <thead>
                <tr>
                  <SortHeader table={awaiting} column="task">
                    Task
                  </SortHeader>
                  <SortHeader table={awaiting} column="stage">
                    Stage
                  </SortHeader>
                  <SortHeader table={awaiting} column="needs">
                    Needs
                  </SortHeader>
                </tr>
              </thead>
              <tbody>
                {awaiting.rows.map((task) => (
                  <tr key={task.task_id}>
                    <td className="font-mono text-xs">{task.task_id.slice(0, 8)}</td>
                    <td className="font-mono text-xs">{task.stage}</td>
                    <td>{task.action ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            </>
          )}
        </Panel>
      </div>

      <Panel title="Dead letters" flush>
        {dead.total === 0 ? (
          <div className="px-5 py-3">
            <Empty>No task has exhausted its retries.</Empty>
          </div>
        ) : (
          <>
          <TableSearch table={dead} label="dead letters" />
          <table className="data-table">
            <thead>
              <tr>
                <SortHeader table={dead} column="task">
                  Task
                </SortHeader>
                <SortHeader table={dead} column="stage">
                  Stage
                </SortHeader>
                <SortHeader table={dead} column="error">
                  Last error
                </SortHeader>
                <th>
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {dead.rows.map((task) => (
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
          </>
        )}
      </Panel>

      <Panel title={`Audit log (latest ${AUDIT_LIMIT})`} flush>
        {audit.total === 0 ? (
          <div className="px-5 py-3">
            <Empty>No audit events yet.</Empty>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <TableSearch table={audit} label="audit events" placeholder="Filter by action, subject or detail" />
            <table className="data-table">
              <thead>
                <tr>
                  <SortHeader table={audit} column="when">
                    When
                  </SortHeader>
                  <SortHeader table={audit} column="action">
                    Action
                  </SortHeader>
                  <SortHeader table={audit} column="subject">
                    Subject
                  </SortHeader>
                  <SortHeader table={audit} column="detail">
                    Detail
                  </SortHeader>
                </tr>
              </thead>
              <tbody>
                {audit.rows.map((event) => (
                  <tr key={event.id}>
                    <td className="whitespace-nowrap text-xs text-muted" title={event.occurred_at}>
                      {new Date(event.occurred_at).toLocaleString()}
                    </td>
                    <td>
                      <StateChip state={event.action} kind="audit_action" />
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
