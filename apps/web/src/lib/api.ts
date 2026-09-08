/**
 * Typed API client.
 *
 * Every call goes to a same-origin route under `/api`. The route handler attaches
 * the `httpOnly` session cookie server-side, so the browser never holds a bearer
 * token and a stolen `localStorage` is not a session.
 *
 * Errors arrive as problem details; `ApiError` carries `errorClass` so callers
 * branch on a stable code instead of matching prose.
 */

import type {
  AcquisitionPlan,
  AskEvent,
  Candidate,
  ChatMessage,
  ChatSession,
  CorpusStats,
  Me,
  ProblemDetails,
  Release,
  ReviewItem,
} from "@/types";
import { appPath } from "@/lib/config";

export class ApiError extends Error {
  readonly status: number;
  readonly errorClass: string;

  constructor(problem: ProblemDetails) {
    super(problem.detail);
    this.name = "ApiError";
    this.status = problem.status;
    this.errorClass = problem.error_class;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(appPath(`/api/backend${path}`), {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init.headers ?? {}),
    },
  });
  if (!response.ok) {
    let problem: ProblemDetails = {
      type: "about:blank",
      title: response.statusText,
      status: response.status,
      detail: response.statusText,
      error_class: "internal",
    };
    try {
      problem = { ...problem, ...(await response.json()) };
    } catch {
      // A non-JSON error body stays as the status text: better a plain message
      // than a parse error masking the real failure.
    }
    throw new ApiError(problem);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

// ── auth ──────────────────────────────────────────────────────────────────
export async function login(
  email: string,
  password: string,
  workspace?: string,
): Promise<{ workspaces: { id: string; slug: string; name: string }[] }> {
  const response = await fetch(appPath("/api/auth/login"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password, workspace }),
  });
  if (!response.ok) {
    const problem = (await response.json().catch(() => ({}))) as Partial<ProblemDetails>;
    throw new ApiError({
      type: "about:blank",
      title: "login failed",
      status: response.status,
      detail: problem.detail ?? "email or password is incorrect",
      error_class: problem.error_class ?? "authentication_required",
    });
  }
  return (await response.json()) as { workspaces: { id: string; slug: string; name: string }[] };
}

export async function logout(): Promise<void> {
  await fetch(appPath("/api/auth/logout"), { method: "POST" });
}

export const getMe = () => request<Me>("/auth/me");

// ── ask ───────────────────────────────────────────────────────────────────
export const createSession = () =>
  request<ChatSession>("/chat/sessions", { method: "POST" });

export const listSessions = () =>
  request<{ items: ChatSession[] }>("/chat/sessions").then((body) => body.items);

export const getSession = (sessionId: string) =>
  request<ChatSession & { messages: ChatMessage[] }>(`/chat/sessions/${sessionId}`);

export const renameSession = (sessionId: string, title: string) =>
  request<ChatSession>(`/chat/sessions/${sessionId}`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  });

export const deleteSession = (sessionId: string) =>
  request<void>(`/chat/sessions/${sessionId}`, { method: "DELETE" });

/**
 * Stream an answer.
 *
 * Consumes the server-sent event stream with `fetch` + `ReadableStream` rather
 * than `EventSource`, because `EventSource` cannot POST and cannot send the
 * workspace header.
 */
export async function ask(
  sessionId: string,
  question: string,
  onEvent: (event: AskEvent) => void,
  options: { topK?: number; signal?: AbortSignal } = {},
): Promise<void> {
  const response = await fetch(appPath(`/api/backend/chat/sessions/${sessionId}/messages`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, top_k: options.topK }),
    signal: options.signal,
  });
  if (!response.ok || !response.body) {
    throw new ApiError({
      type: "about:blank",
      title: "ask failed",
      status: response.status,
      detail: "the answer stream could not be started",
      error_class: "internal",
    });
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // SSE frames are separated by a blank line; a partial frame stays buffered.
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const parsed = parseFrame(frame);
      if (parsed) onEvent(parsed);
      boundary = buffer.indexOf("\n\n");
    }
  }
}

export function parseFrame(frame: string): AskEvent | null {
  let event = "";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event: ")) event = line.slice(7).trim();
    else if (line.startsWith("data: ")) dataLines.push(line.slice(6));
  }
  if (!event) return null;
  const raw = dataLines.join("\n");
  if (event === "token") {
    return { type: "token", data: raw };
  }
  try {
    return { type: event, data: JSON.parse(raw) } as AskEvent;
  } catch {
    return null;
  }
}

export const resolveCitation = (messageId: string, handle: string) =>
  request<Record<string, unknown>>(`/chat/messages/${messageId}/citations/${handle}`);

export const sendFeedback = (
  messageId: string,
  rating: number,
  comment?: string,
  flaggedHandles: string[] = [],
) =>
  request<{ id: string }>("/feedback", {
    method: "POST",
    body: JSON.stringify({
      message_id: messageId,
      rating,
      comment,
      flagged_handles: flaggedHandles,
    }),
  });

// ── search ────────────────────────────────────────────────────────────────
export const search = (query: string, topK?: number) =>
  request<{
    release_id: string;
    classification: string;
    withheld_by_policy: number | null;
    results_filtered: boolean;
    results: {
      handle: string;
      text: string;
      title: string | null;
      year: number | null;
      source_label: string | null;
      locator: string | null;
      display_restricted: boolean;
      work_id: string;
    }[];
  }>("/search", { method: "POST", body: JSON.stringify({ query, top_k: topK }) });

// ── collections ───────────────────────────────────────────────────────────
export const listCandidates = () =>
  request<{ items: Candidate[] }>("/candidates").then((body) => body.items);

export const listConflicts = () =>
  request<{
    conflicts: { work_id: string }[];
    duplicate_proposals: {
      left_work_id: string;
      right_work_id: string;
      similarity: number;
      features: Record<string, unknown>;
      reason_code: string;
    }[];
  }>("/candidates/conflicts");

export const setRelevance = (workId: string, relevance: string, reasonCode: string) =>
  request<{ work_id: string; relevance: string }>(`/candidates/${workId}/relevance`, {
    method: "POST",
    body: JSON.stringify({ relevance, reason_code: reasonCode }),
  });

export const mergeWorks = (keepWorkId: string, mergeWorkId: string, reasonCode: string) =>
  request<{ decision_id: string; after: Record<string, unknown> }>("/decisions/merge", {
    method: "POST",
    body: JSON.stringify({ keep_work_id: keepWorkId, merge_work_id: mergeWorkId, reason_code: reasonCode }),
  });

export const listPlans = (status?: string) =>
  request<{ items: AcquisitionPlan[] }>(
    `/acquisition-plans${status ? `?status=${encodeURIComponent(status)}` : ""}`,
  ).then((body) => body.items);

/**
 * Upload an authorised copy against a queued candidate.
 *
 * The same plan, the same validations, and downstream work resumes: this is the
 * assisted loop closing, not a detached upload page.
 */
export async function assistedUpload(
  planId: string,
  form: {
    manifestationId: string;
    accessBasis: string;
    attestation: string;
    file: File;
  },
): Promise<{ asset_id: string; identity_verified: boolean; malware_scan_status: string }> {
  const body = new FormData();
  body.set("manifestation_id", form.manifestationId);
  body.set("access_basis", form.accessBasis);
  body.set("attestation", form.attestation);
  body.set("upload", form.file);
  const response = await fetch(appPath(`/api/backend/assisted-acquisitions/${planId}`), {
    method: "POST",
    body,
  });
  if (!response.ok) {
    const problem = (await response.json().catch(() => ({}))) as Partial<ProblemDetails>;
    throw new ApiError({
      type: "about:blank",
      title: "upload rejected",
      status: response.status,
      detail: problem.detail ?? "the upload was rejected",
      error_class: problem.error_class ?? "invalid_content",
    });
  }
  return (await response.json()) as {
    asset_id: string;
    identity_verified: boolean;
    malware_scan_status: string;
  };
}

// ── review ────────────────────────────────────────────────────────────────
export const listReviewQueue = (includeInvalid = false) =>
  request<{ items: ReviewItem[] }>(
    `/claims?include_invalid=${includeInvalid ? "true" : "false"}`,
  ).then((body) => body.items);

export const decideReview = (
  revisionId: string,
  payload: {
    decision: "accept" | "correct" | "reject" | "defer" | "dispute" | "request_reparse";
    reason_code: string;
    comment?: string;
    corrected_value?: Record<string, unknown>;
    expected_revision_number?: number;
    reviewer_interpretation?: boolean;
  },
) =>
  request<{ revision_id: string; revision_number: number; review_status: string }>(
    `/reviews/${revisionId}`,
    { method: "POST", body: JSON.stringify(payload) },
  );

export const correctionRate = () => request<Record<string, number>>("/reviews/correction-rate");

// ── releases ──────────────────────────────────────────────────────────────
export const listReleases = () =>
  request<{
    items: Release[];
    aliases: { alias: string; release_id: string | null; previous_release_id: string | null }[];
  }>("/corpus-releases");

export const draftRelease = (payload: {
  name: string;
  semantic_version: string;
  include_claims?: boolean;
  parent_release_id?: string;
}) =>
  request<{
    release_id: string;
    status: string;
    manifest_sha256: string;
    items: number;
    diff: Record<string, number | boolean>;
  }>("/corpus-releases", { method: "POST", body: JSON.stringify(payload) });

export const validateRelease = (releaseId: string) =>
  request<{ passed: boolean; gates: Record<string, boolean>; failures: string[] }>(
    `/corpus-releases/${releaseId}/validate`,
    { method: "POST" },
  );

export const approveRelease = (releaseId: string) =>
  request<{ release_id: string; status: string; indexing_tasks: string[] }>(
    `/corpus-releases/${releaseId}/approve`,
    { method: "POST", body: JSON.stringify({ evaluation_run_id: null }) },
  );

export const publishRelease = (releaseId: string, alias = "current") =>
  request<{ alias: string; release_id: string }>(
    `/corpus-releases/${releaseId}/publish?alias=${encodeURIComponent(alias)}`,
    { method: "POST" },
  );

export const rollbackRelease = (alias = "current") =>
  request<{ alias: string; release_id: string }>(
    `/corpus-releases/rollback?alias=${encodeURIComponent(alias)}`,
    { method: "POST" },
  );

export const buildExport = (releaseId: string) =>
  request<{ valid: boolean; problems: string[]; row_counts: Record<string, number>; files: string[] }>(
    `/exports/${releaseId}`,
    { method: "POST" },
  );

// ── analytics and operations ──────────────────────────────────────────────
export const corpusStats = () => request<CorpusStats>("/analytics/corpus");

export const temporal = () =>
  request<{ items: { year: number; count: number }[] }>("/analytics/temporal").then((b) => b.items);

export const journals = () =>
  request<{ items: { journal: string; count: number }[] }>("/analytics/journals").then(
    (b) => b.items,
  );

export const subjectTerms = () =>
  request<{ items: { term: string; count: number }[] }>("/analytics/subject-terms").then(
    (b) => b.items,
  );

export const sourceContribution = () =>
  request<{ items: { connector_id: string; works: number; unique_contribution: number }[] }>(
    "/analytics/sources",
  ).then((b) => b.items);

export const taskSnapshot = () =>
  request<{
    counts: Record<string, number>;
    oldest_pending_by_stage: Record<string, string>;
    awaiting_user: { task_id: string; stage: string; action: string | null }[];
    dead_letters: { task_id: string; stage: string; error: string | null }[];
  }>("/operations/tasks");
