/**
 * Watching a queued task from a page.
 *
 * Two presses now queue work instead of holding the request open: the fetch
 * sweep and deleting a scope. Both read the workspace for minutes, and both used
 * to be cut off by the gateway with the work half done and nothing said. The page
 * asks after the task instead, and what it says at each ending is the whole of
 * what a person sees, so the endings live in one place rather than two.
 */

/** What a task-status route answers, whatever the task was. */
export interface TaskWatch<T> {
  task_id: string;
  state: string;
  settled: boolean;
  counts: T | null;
  error: string | null;
}

/** How often to ask, and for how long: ten minutes at two seconds. */
export const WATCH_POLL_MS = 2_000;
export const WATCH_ROUNDS = 300;

/**
 * Ask after `taskId` until it settles, and say what happened.
 *
 * A failed poll is ignored rather than ending the watch: the task is durable, and
 * an API restarting under a deploy is exactly the thing it outlives. Running out
 * of rounds never claims the work finished — it says the task is still running,
 * or, when nothing has claimed it, that nothing has, which on this deployment
 * means the worker is down.
 */
export async function watchTask<T>({
  poll,
  said,
  stillRunning,
}: {
  poll: () => Promise<TaskWatch<T>>;
  /** What to say once it has succeeded, from the counts it reported. */
  said: (counts: T) => string;
  /** What to point at while it is still going. */
  stillRunning: string;
}): Promise<string> {
  let last = "ready";
  for (let round = 0; round < WATCH_ROUNDS; round++) {
    // Asked before the first wait, not after it: a small workspace finishes in
    // under a second, and making it sit through a poll interval to be told so is
    // the delay this whole change exists to remove.
    if (round > 0) await new Promise((resolve) => setTimeout(resolve, WATCH_POLL_MS));
    let status: TaskWatch<T>;
    try {
      status = await poll();
    } catch {
      continue;
    }
    last = status.state;
    if (!status.settled) continue;
    if (!status.counts) {
      return (
        status.error ??
        "It stopped before it finished. Nothing it had already done is lost; press again to carry on."
      );
    }
    return said(status.counts);
  }
  return last === "ready" || last === "pending"
    ? "Still waiting to start after ten minutes: nothing has picked it up. " +
        "Check the worker queue under Operations."
    : `Still running after ten minutes. It carries on in the background: ${stillRunning}`;
}
