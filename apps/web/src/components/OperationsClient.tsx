"use client";

import { type FormEvent, useCallback, useEffect, useState } from "react";

import { InfoTip } from "@/components/ui/InfoTip";
import { LiveStatus } from "@/components/ui/LiveStatus";
import { Pager } from "@/components/ui/Pager";
import { ServerSearch } from "@/components/ui/ServerSearch";
import { CollapsiblePanel } from "@/components/ui/CollapsiblePanel";
import { Alert, Button, Empty, Input, StateChip } from "@/components/ui/primitives";
import { SortHeader } from "@/components/ui/table";
import { listAuditEvents, retryTask, runningWork, taskQueue, taskSnapshot } from "@/lib/api";
import { describeStatus } from "@/lib/statuses";
import { useLiveRefresh } from "@/lib/useLiveRefresh";
import { usePager } from "@/lib/usePager";
import { useTable } from "@/lib/useTable";
import { utcWhen } from "@/lib/when";
import type { AuditEvent, Page, QueueTask, RunningWork, TaskQueueName, TaskSnapshot } from "@/types";

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
/** Rows a task list shows at a time; the audit log uses the app's usual 50. */
const QUEUE_PAGE = 10;

const selectClass = "rounded-control border border-border-strong bg-surface px-2 py-1.5 text-sm text-ink";

function ago(iso: string | null): string {
  if (!iso) return "—";
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

function message(caught: unknown, fallback: string): string {
  return caught instanceof Error ? caught.message : fallback;
}

/**
 * One page of a task queue, reloaded whenever `tick` moves, so the page's live
 * refresh keeps every list current without losing the page you are on.
 */
function useQueuePage(queue: TaskQueueName, tick: number, stage: string | null = null) {
  const pager = usePager(QUEUE_PAGE);
  const [page, setPage] = useState<Page<QueueTask> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const { cursor, pageSize, reset } = pager;
  // A search runs on the server over every task in the queue, from the first page.
  const search = useCallback(
    (words: string) => {
      setQuery(words);
      reset();
    },
    [reset],
  );
  useEffect(() => {
    // Nothing until the page's first refresh, which moves `tick` to 1.
    if (tick === 0) return;
    let live = true;
    taskQueue(queue, { stage, after: cursor, limit: pageSize, q: query || null })
      .then((next) => {
        if (!live) return;
        setPage(next);
        setError(null);
      })
      .catch((caught) => {
        if (live) setError(message(caught, "the tasks could not be loaded"));
      });
    return () => {
      live = false;
    };
  }, [queue, stage, cursor, pageSize, tick, query]);
  return { pager, page, error, query, search };
}

/** The search box above a task list. */
function QueueSearch({ label, query, onSearch }: { label: string; query: string; onSearch: (q: string) => void }) {
  return (
    <ServerSearch
      label={label}
      placeholder="Words, a task id, or a date, e.g., 2026-11-23"
      query={query}
      onSearch={onSearch}
      className="border-b border-border px-5 py-2.5"
    />
  );
}

function QueueError({ error }: { error: string | null }) {
  return error ? (
    <div className="px-5 pt-3">
      <Alert tone="danger" role="alert">
        {error}
      </Alert>
    </div>
  ) : null;
}

/**
 * Operations: the durable task queue and the audit trail.
 *
 * Everything here is read from task rows and audit events, never inferred from a
 * worker heartbeat: a task is finished when its row says so. Every list is paged
 * by the API, so counts and ages are over every task, not over a sample. The
 * page refreshes itself every 15 seconds and keeps each list on the page you are
 * reading; retrying a dead-lettered task puts it back in the queue, where the
 * normal lease and retry policy applies again.
 */
export function OperationsClient() {
  const [snapshot, setSnapshot] = useState<TaskSnapshot | null>(null);
  const [tick, setTick] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setSnapshot(await taskSnapshot());
      setTick((value) => value + 1);
      setError(null);
    } catch (caught) {
      setError(message(caught, "operations could not be loaded"));
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
      report = { ok: false, message: message(caught, "the task was not retried") };
    } finally {
      setBusy(null);
    }
    // Reported after the reload, which clears the error banner when it succeeds.
    live.kick();
    await live.refresh();
    if (report.ok) setNotice(report.message);
    else setError(report.message);
  }

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

      {/* One above the other, not side by side: each row carries a stage, a state,
          a time and an action, which wrapped badly in half a screen. Now that the
          panels fold away, full width costs nothing when they are not wanted. */}
      <RunningNow tick={tick} />

      <WaitingLongest stages={snapshot?.waiting_by_stage ?? []} tick={tick} />

      <WaitingForPerson tick={tick} />

      <DeadLetters tick={tick} busy={busy} onRetry={retry} />

      <AuditLog tick={tick} />
    </div>
  );
}

/**
 * What each worker is doing now, in words, and why that many run at once.
 *
 * A worker runs one task at a time, for one class of stages, and each stage has
 * a limit across all workers: the smaller of the two decides. Both are shown, so
 * a limit that cannot bind (higher than the workers of its class) is visible.
 */
function RunningNow({ tick }: { tick: number }) {
  const [work, setWork] = useState<RunningWork | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    runningWork()
      .then((next) => {
        if (!current) return;
        setWork(next);
        setError(null);
      })
      .catch((caught) => current && setError(message(caught, "the running tasks could not be loaded")));
    return () => {
      current = false;
    };
  }, [tick]);

  const running = useTable(work?.running ?? [], {
    columns: {
      worker: (task) => task.worker ?? "",
      doing: (task) => task.doing,
      stage: (task) => task.stage,
      attempt: (task) => task.attempt,
      started: (task) => task.first_started_at,
      heartbeat: (task) => task.heartbeat_at,
    },
  });
  const stages = useTable(work?.stages ?? [], {
    columns: {
      stage: (row) => row.stage,
      class: (row) => row.worker_class,
      running: (row) => row.running,
      limit: (row) => row.limit,
      ready: (row) => row.ready,
    },
  });
  const busy = (workerClass: string) =>
    (work?.busy_workers ?? []).filter((worker) => worker.worker_class === workerClass).length;
  const classStages = (workerClass: string) =>
    (work?.stages ?? []).filter((row) => row.worker_class === workerClass).map((row) => row.stage).join(", ");

  return (
    <CollapsiblePanel
      title="Running now"
      storageKey="operations.running"
      count={
        work ? (
          <span className="text-xs text-muted">
            {work.running.length.toLocaleString()} task{work.running.length === 1 ? "" : "s"}
          </span>
        ) : undefined
      }
      flush
    >
      <QueueError error={error} />
      {work === null ? (
        <p className="px-5 py-3 text-sm text-muted">Loading…</p>
      ) : work.running.length === 0 ? (
        <div className="px-5 py-3">
          <Empty>No worker is running a task.</Empty>
        </div>
      ) : (
        <table className="data-table" aria-label="Running tasks">
          <thead>
            <tr>
              <SortHeader table={running} column="worker">
                Worker
              </SortHeader>
              <SortHeader table={running} column="doing">
                Doing
              </SortHeader>
              <SortHeader table={running} column="stage">
                Stage
              </SortHeader>
              <SortHeader table={running} column="attempt" numeric>
                Attempt
              </SortHeader>
              <SortHeader table={running} column="started">
                First started
              </SortHeader>
              <SortHeader table={running} column="heartbeat" numeric>
                Last heartbeat
              </SortHeader>
            </tr>
          </thead>
          <tbody>
            {running.rows.map((task) => (
              <tr key={task.task_id}>
                <td className="font-mono text-xs">{task.worker ?? "—"}</td>
                <td className="text-sm">
                  <span className="text-ink">{task.doing}</span>
                  {task.paper && task.paper.dois.length > 0 ? (
                    <span className="block text-xs text-muted">
                      DOI{" "}
                      <a
                        className="font-mono text-link underline"
                        href={`https://doi.org/${task.paper.dois[0]}`}
                        target="_blank"
                        rel="noreferrer"
                      >
                        {task.paper.dois[0]}
                      </a>
                    </span>
                  ) : null}
                  {task.stalled ? (
                    <span className="mt-1 block">
                      <span className="rounded-control border border-warning-strong/25 bg-warning-lt px-1.5 py-0.5 text-xs font-semibold text-warning-strong">
                        Stalled
                      </span>{" "}
                      <span className="text-xs text-muted">
                        its worker stopped renewing the lease; the next free {task.worker_class} worker takes it over
                      </span>
                    </span>
                  ) : null}
                </td>
                <td className="font-mono text-xs">{task.stage}</td>
                <td className="numeric">
                  {task.attempt} of {task.max_attempts}
                </td>
                <td className="whitespace-nowrap text-xs text-muted">{utcWhen(task.first_started_at)}</td>
                <td className="numeric" title={task.heartbeat_at ?? undefined}>
                  {ago(task.heartbeat_at)} ago
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {work ? (
        <section aria-label="How many run at once" className="space-y-3 border-t border-border px-5 py-3">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-heading">How many run at once</h3>
          <div className="max-w-3xl space-y-2 text-xs text-muted">
            <p>
              Each worker runs <strong>one task at a time</strong>, for one class of stages:{" "}
              <strong>network</strong> ({classStages("network") || "none"}) or <strong>compute</strong> (
              {classStages("compute") || "none"}). Each stage also has a <strong>limit</strong> across all workers,
              and the smaller of the two decides: a limit higher than the workers of its class has no effect. A
              worker that finds a stage at its limit takes the next stage of its class instead. Busy now:{" "}
              {busy("network")} network and {busy("compute")} compute worker
              {busy("network") + busy("compute") === 1 ? "" : "s"}; an idle worker holds no task, so it is not listed.
            </p>
            <p>
              To change it, an administrator edits <code className="font-mono">stage_concurrency</code> in the
              deployment&apos;s settings (<code className="font-mono">infra/environments/server.yaml</code>) or runs
              more <code className="font-mono">worker-network</code> or{" "}
              <code className="font-mono">worker-compute</code> containers, then restarts them. A worker renews its
              lease while it works; one silent for {Math.round(work.lease_seconds / 60)} min is stalled, and its
              task is taken over.
            </p>
          </div>
          <table className="data-table" aria-label="Stage limits">
            <thead>
              <tr>
                <SortHeader table={stages} column="stage">
                  Stage
                </SortHeader>
                <SortHeader table={stages} column="class">
                  Worker class
                </SortHeader>
                <SortHeader table={stages} column="running" numeric>
                  Running
                </SortHeader>
                <SortHeader table={stages} column="limit" numeric>
                  Limit
                </SortHeader>
                <SortHeader table={stages} column="ready" numeric>
                  Waiting
                </SortHeader>
              </tr>
            </thead>
            <tbody>
              {stages.rows.map((row) => (
                <tr key={row.stage}>
                  <td className="font-mono text-xs">{row.stage}</td>
                  <td className="font-mono text-xs">{row.worker_class}</td>
                  <td className="numeric">{row.running.toLocaleString()}</td>
                  <td className="numeric">{row.limit.toLocaleString()}</td>
                  <td className="numeric">{row.ready.toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ) : null}
    </CollapsiblePanel>
  );
}

/** Tasks ready to run or held by a worker, the one that has waited longest first. */
function WaitingLongest({ stages, tick }: { stages: TaskSnapshot["waiting_by_stage"]; tick: number }) {
  const [stage, setStage] = useState<string>("");
  const { pager, page, error, query, search } = useQueuePage("waiting", tick, stage || null);
  const table = useTable(page?.items ?? [], {
    columns: {
      task: (task) => task.task_id,
      stage: (task) => task.stage,
      state: (task) => task.state,
      when: (task) => task.created_at,
      since: (task) => task.created_at,
    },
  });
  const all = stages.reduce((sum, row) => sum + row.count, 0);
  return (
    <CollapsiblePanel
      title="Waiting longest"
      storageKey="operations.waiting-longest"
      flush
      aside={
        <select
          aria-label="Waiting tasks in stage"
          className={selectClass}
          value={stage}
          onChange={(event) => {
            setStage(event.target.value);
            pager.reset();
          }}
        >
          <option value="">All stages ({all.toLocaleString()})</option>
          {stages.map((row) => (
            <option key={row.stage} value={row.stage}>
              {row.stage} ({row.count.toLocaleString()}, oldest {ago(row.oldest)})
            </option>
          ))}
        </select>
      }
    >
      <QueueError error={error} />
      <QueueSearch label="waiting tasks" query={query} onSearch={search} />
      {page === null ? (
        <p className="px-5 py-3 text-sm text-muted">Loading…</p>
      ) : page.total === 0 ? (
        <div className="px-5 py-3">
          <Empty>{query ? "No waiting task holds every word." : "Nothing is waiting to run."}</Empty>
        </div>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <SortHeader table={table} column="task">
                Task
              </SortHeader>
              <SortHeader table={table} column="stage">
                Stage
              </SortHeader>
              <SortHeader table={table} column="state">
                State
              </SortHeader>
              <SortHeader table={table} column="when">
                When
              </SortHeader>
              <SortHeader table={table} column="since" numeric>
                Waiting
              </SortHeader>
            </tr>
          </thead>
          <tbody>
            {table.rows.map((task) => (
              <tr key={task.task_id}>
                <td className="font-mono text-xs">{task.task_id.slice(0, 8)}</td>
                <td className="font-mono text-xs">{task.stage}</td>
                <td>
                  <StateChip state={task.state} kind="task" />
                </td>
                <td className="whitespace-nowrap text-xs text-muted">{utcWhen(task.created_at)}</td>
                <td className="numeric" title={task.created_at ?? undefined}>
                  {ago(task.created_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {page ? (
        <Pager pager={pager} shown={page.items.length} total={page.total} nextCursor={page.next_cursor} label="waiting tasks" />
      ) : null}
    </CollapsiblePanel>
  );
}

/** Tasks stopped until a person decides, the longest-waiting first. */
function WaitingForPerson({ tick }: { tick: number }) {
  const { pager, page, error, query, search } = useQueuePage("awaiting_user", tick);
  const table = useTable(page?.items ?? [], {
    columns: {
      task: (task) => task.task_id,
      stage: (task) => task.stage,
      needs: (task) => task.action,
      when: (task) => task.created_at,
      since: (task) => task.created_at,
    },
  });
  return (
    <CollapsiblePanel title="Waiting for a person" storageKey="operations.waiting-person" flush>
      <QueueError error={error} />
      <QueueSearch label="tasks waiting for a person" query={query} onSearch={search} />
      {page === null ? (
        <p className="px-5 py-3 text-sm text-muted">Loading…</p>
      ) : page.total === 0 ? (
        <div className="px-5 py-3">
          <Empty>{query ? "No task waiting for a person holds every word." : "No task needs a decision."}</Empty>
        </div>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <SortHeader table={table} column="task">
                Task
              </SortHeader>
              <SortHeader table={table} column="stage">
                Stage
              </SortHeader>
              <SortHeader table={table} column="needs">
                Needs
              </SortHeader>
              <SortHeader table={table} column="when">
                When
              </SortHeader>
              <SortHeader table={table} column="since" numeric>
                Waiting
              </SortHeader>
            </tr>
          </thead>
          <tbody>
            {table.rows.map((task) => (
              <tr key={task.task_id}>
                <td className="font-mono text-xs">{task.task_id.slice(0, 8)}</td>
                <td className="font-mono text-xs">{task.stage}</td>
                <td>{task.action ?? "—"}</td>
                <td className="whitespace-nowrap text-xs text-muted">{utcWhen(task.created_at)}</td>
                <td className="numeric" title={task.created_at ?? undefined}>
                  {ago(task.created_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {page ? (
        <Pager
          pager={pager}
          shown={page.items.length}
          total={page.total}
          nextCursor={page.next_cursor}
          label="tasks waiting for a person"
        />
      ) : null}
    </CollapsiblePanel>
  );
}

function DeadLetters({
  tick,
  busy,
  onRetry,
}: {
  tick: number;
  busy: string | null;
  onRetry: (taskId: string) => void;
}) {
  const { pager, page, error, query, search } = useQueuePage("dead_letter", tick);
  const table = useTable(page?.items ?? [], {
    columns: {
      task: (task) => task.task_id,
      stage: (task) => task.stage,
      when: (task) => task.completed_at ?? task.created_at,
      error: (task) => task.error,
    },
  });
  return (
    <CollapsiblePanel title="Dead letters" storageKey="operations.dead-letters" flush>
      <QueueError error={error} />
      <QueueSearch label="dead letters" query={query} onSearch={search} />
      {page === null ? (
        <p className="px-5 py-3 text-sm text-muted">Loading…</p>
      ) : page.total === 0 ? (
        <div className="px-5 py-3">
          <Empty>{query ? "No dead letter holds every word." : "No task has exhausted its retries."}</Empty>
        </div>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <SortHeader table={table} column="task">
                Task
              </SortHeader>
              <SortHeader table={table} column="stage">
                Stage
              </SortHeader>
              <SortHeader table={table} column="when">
                When
              </SortHeader>
              <SortHeader table={table} column="error">
                Last error
              </SortHeader>
              <th>
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {table.rows.map((task) => (
              <tr key={task.task_id}>
                <td className="font-mono text-xs">{task.task_id.slice(0, 8)}</td>
                <td className="font-mono text-xs">{task.stage}</td>
                <td
                  className="whitespace-nowrap text-xs text-muted"
                  title={task.completed_at ? "When it ran out of retries" : "When it was queued"}
                >
                  {utcWhen(task.completed_at ?? task.created_at)}
                </td>
                <td className="max-w-md break-words text-xs text-danger-strong">{task.error ?? "—"}</td>
                <td>
                  <div className="flex justify-end">
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={busy === task.task_id}
                      onClick={() => onRetry(task.task_id)}
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
      {page ? (
        <Pager pager={pager} shown={page.items.length} total={page.total} nextCursor={page.next_cursor} label="dead letters" />
      ) : null}
    </CollapsiblePanel>
  );
}

/**
 * The audit trail, newest first, a page at a time. The search and the action
 * filter run on the server over every event, not over the page on screen.
 */
function AuditLog({ tick }: { tick: number }) {
  const pager = usePager();
  const [page, setPage] = useState<(Page<AuditEvent> & { action_counts: Record<string, number> }) | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [action, setAction] = useState("");
  const [typed, setTyped] = useState("");
  const [query, setQuery] = useState("");
  const { cursor, pageSize, reset } = pager;

  useEffect(() => {
    if (tick === 0) return;
    let live = true;
    listAuditEvents({ after: cursor, limit: pageSize, action: action || null, q: query || null })
      .then((next) => {
        if (!live) return;
        setPage(next);
        setError(null);
      })
      .catch((caught) => {
        if (live) setError(message(caught, "the audit log could not be loaded"));
      });
    return () => {
      live = false;
    };
  }, [cursor, pageSize, action, query, tick]);

  const table = useTable(page?.items ?? [], {
    columns: {
      when: (event) => event.occurred_at,
      action: (event) => event.action,
      subject: (event) => `${event.subject_type}:${event.subject_id ?? ""}`,
      detail: (event) => event.reason_code,
    },
  });

  function search(event: FormEvent) {
    event.preventDefault();
    setQuery(typed.trim());
    reset();
  }

  const counts = page?.action_counts ?? {};
  const everything = Object.values(counts).reduce((sum, count) => sum + count, 0);
  const filtered = Boolean(action || query);

  return (
    <CollapsiblePanel title="Audit log" storageKey="operations.audit" flush>
      <form
        role="search"
        aria-label="Search the audit log"
        onSubmit={search}
        className="flex flex-wrap items-center gap-3 border-b border-border px-5 py-2.5"
      >
        <select
          aria-label="Filter by action"
          className={selectClass}
          value={action}
          onChange={(event) => {
            setAction(event.target.value);
            reset();
          }}
        >
          <option value="">Every action ({everything.toLocaleString()})</option>
          {Object.entries(counts).map(([name, count]) => (
            <option key={name} value={name}>
              {name} ({count.toLocaleString()})
            </option>
          ))}
        </select>
        <Input
          type="search"
          aria-label="Search audit events"
          placeholder="Words in the action, subject or details, or a date, e.g., 2026-11-23"
          className="w-full max-w-xs"
          value={typed}
          onChange={(event) => setTyped(event.target.value)}
        />
        <Button type="submit" variant="ghost" size="sm">
          Search
        </Button>
        {filtered ? (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => {
              setAction("");
              setTyped("");
              setQuery("");
              reset();
            }}
          >
            Clear
          </Button>
        ) : null}
      </form>
      {error ? (
        <div className="px-5 pt-3">
          <Alert tone="danger" role="alert">
            {error}
          </Alert>
        </div>
      ) : null}
      {page === null ? (
        <p className="px-5 py-3 text-sm text-muted">Loading…</p>
      ) : page.total === 0 ? (
        <div className="px-5 py-3">
          <Empty>{filtered ? "No audit event matches." : "No audit events yet."}</Empty>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="data-table">
            <thead>
              <tr>
                <SortHeader table={table} column="when">
                  When
                </SortHeader>
                <SortHeader table={table} column="action">
                  Action
                </SortHeader>
                <SortHeader table={table} column="subject">
                  Subject
                </SortHeader>
                <SortHeader table={table} column="detail">
                  Detail
                </SortHeader>
              </tr>
            </thead>
            <tbody>
              {table.rows.map((event) => (
                <tr key={event.id}>
                  <td className="whitespace-nowrap text-xs text-muted" title={event.occurred_at}>
                    {utcWhen(event.occurred_at)}
                  </td>
                  <td>
                    <StateChip state={event.action} kind="audit_action" />
                  </td>
                  <td className="font-mono text-xs">
                    {event.subject_type}
                    {event.subject_id ? `:${event.subject_id.slice(0, 8)}` : ""}
                  </td>
                  <td className="max-w-lg break-words text-xs text-muted">
                    {[event.reason_code, summarise(event.metadata)].filter(Boolean).join(" · ") || "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {page ? (
        <Pager pager={pager} shown={page.items.length} total={page.total} nextCursor={page.next_cursor} label="audit events" />
      ) : null}
    </CollapsiblePanel>
  );
}
