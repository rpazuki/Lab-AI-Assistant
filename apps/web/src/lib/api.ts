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
  Allowance,
  AnswerReview,
  AskEvent,
  AuditEvent,
  Benchmark,
  BulkRelevanceResult,
  Candidate,
  CandidateFacets,
  CandidateFilters,
  ChatMessage,
  ChatSession,
  CorpusStats,
  DiscoveryRun,
  DuplicateProposal,
  EvaluationRun,
  ExtractionEstimate,
  ExtractionRun,
  ExtractionTemplate,
  GateStatus,
  Invitation,
  InvitationPreview,
  Me,
  Member,
  MemberSession,
  MergePlan,
  MergeRule,
  Page,
  ParseLimits,
  ParseQuality,
  ProblemDetails,
  QueueTask,
  Release,
  ReleaseGate,
  Reviewer,
  ReviewItem,
  ReviewStep,
  RunComparison,
  RunReviewSummary,
  ScopeDeletionPlan,
  ScopeGateReport,
  ScopeRulesPlan,
  ScopeVersion,
  TaskQueueName,
  TaskSnapshot,
  WorkingSheet,
} from "@/types";
import { appPath } from "@/lib/config";

export class ApiError extends Error {
  readonly status: number;
  readonly errorClass: string;
  readonly context: Record<string, unknown>;

  constructor(problem: ProblemDetails) {
    super(problem.detail);
    this.name = "ApiError";
    this.status = problem.status;
    this.errorClass = problem.error_class;
    this.context = problem.context ?? {};
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

/** The signed-in member's own token allowance. */
export const getAllowance = () => request<Allowance>("/chat/quota");

// ── ask ───────────────────────────────────────────────────────────────────
export const createSession = () =>
  request<ChatSession>("/chat/sessions", { method: "POST" });

/** Your conversations, newest first, one page at a time. */
export const listSessions = (options: PageRequest = {}) =>
  request<Page<ChatSession>>(`/chat/sessions${queryString({ ...options })}`);

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
    // A refusal before the stream opens (a used-up allowance, a missing
    // capability) is a problem document: say what it says.
    const problem = (await response.json().catch(() => ({}))) as Partial<ProblemDetails>;
    throw new ApiError({
      type: "about:blank",
      title: "ask failed",
      status: response.status,
      detail: problem.detail ?? "the answer stream could not be started",
      error_class: problem.error_class ?? "internal",
      context: problem.context,
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
export const listScopes = () =>
  request<{ items: ScopeVersion[] }>("/scopes").then((body) => body.items);

export interface ScopeDocument {
  name: string;
  scientific_objective: string;
  concepts?: string[];
  exclusion_terms?: string[];
  include_threshold?: number;
  exclude_threshold?: number;
  intended_uses?: string[];
  connectors: { connector_id: string; query: string; required?: boolean }[];
}

/** A new scope, or (with `scope_id`) the next version of an existing one. */
export const createScope = (payload: ScopeDocument & { scope_id?: string }) =>
  request<{ id: string; version: number; checksum: string }>("/scopes", {
    method: "POST",
    body: JSON.stringify(payload),
  });

/** Correct an unapproved version in place; approved versions refuse with 409. */
export const updateScopeDraft = (scopeVersionId: string, payload: ScopeDocument) =>
  request<{ id: string; version: number; checksum: string }>(`/scopes/${scopeVersionId}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });

/** Delete an unapproved version; approved versions refuse with 409. */
export const deleteScopeDraft = (scopeVersionId: string) =>
  request<void>(`/scopes/${scopeVersionId}`, { method: "DELETE" });

export const checkScopeGate = (scopeVersionId: string) =>
  request<ScopeGateReport>(`/scopes/${scopeVersionId}/gate`, { method: "POST" });

export const approveScope = (scopeVersionId: string) =>
  request<{ id: string; approval_state: string }>(`/scopes/${scopeVersionId}/approve`, {
    method: "POST",
  });

/** Discovery runs, newest first, one page at a time. */
export const listDiscoveryRuns = (options: PageRequest = {}) =>
  request<Page<DiscoveryRun>>(`/discovery-runs${queryString({ ...options })}`);

export const startDiscovery = (scopeVersionId: string) =>
  request<{ run_id: string; status: string }>("/discovery-runs", {
    method: "POST",
    body: JSON.stringify({ scope_version_id: scopeVersionId }),
  });

/**
 * Delete a run that produced no candidates in use. Refused with 409, and the reason,
 * while it has work queued, once its frozen candidates exist, or while a later run
 * of the same search may be reusing its pages.
 */
export const deleteDiscoveryRun = (runId: string) =>
  request<void>(`/discovery-runs/${runId}`, { method: "DELETE" });

/** Freeze a run. A degraded run freezes only when the steward accepts the gap. */
export const freezeDiscovery = (runId: string, acceptDegraded = false) =>
  request<{ run_id: string; status: string }>(
    `/discovery-runs/${runId}/freeze${acceptDegraded ? "?accept_degraded=true" : ""}`,
    { method: "POST" },
  );

/** `?a=1&b=2` from the values that are set; empty when none are. */
function queryString(params: Record<string, string | number | null | undefined>): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== "") query.set(key, String(value));
  }
  const text = query.toString();
  return text ? `?${text}` : "";
}

export interface PageRequest {
  after?: string | null;
  limit?: number;
}

function filterQuery(filters: CandidateFilters): Record<string, string | number | null | undefined> {
  const { has_abstract, ...rest } = filters;
  return { ...rest, has_abstract: has_abstract === null || has_abstract === undefined ? null : String(has_abstract) };
}

/** One page of candidates matching the filters. */
export const listCandidates = (options: PageRequest & CandidateFilters = {}) => {
  const { after, limit, ...filters } = options;
  return request<Page<Candidate>>(
    `/candidates${queryString({ after, limit, ...filterQuery(filters) })}`,
  );
};

/** The values the candidate filters can take, and how complete the filter index is. */
export const candidateFacets = () => request<CandidateFacets>("/candidates/facets");

/**
 * Include or exclude every candidate the filters match. `apply: false` is a preview
 * that changes nothing; an apply changes up to 1,000 and says how many remain.
 */
export const bulkRelevance = (
  filters: CandidateFilters,
  body: { relevance: string; only_unreviewed: boolean; apply: boolean; step_id?: string | null },
) =>
  request<BulkRelevanceResult>(`/candidates/relevance/bulk${queryString(filterQuery(filters))}`, {
    method: "POST",
    body: JSON.stringify(body),
  });

/** How many works are in conflict, and one page of open duplicate pairs, most similar first. */
export const listConflicts = (options: PageRequest & { min_similarity?: number | null } = {}) =>
  request<{
    conflict_total: number;
    proposal_total: number;
    next_cursor: string | null;
    duplicate_proposals: DuplicateProposal[];
  }>(`/candidates/conflicts${queryString({ ...options })}`);

/** Merge one open pair; the paper with more identifiers is kept. */
export const mergeDuplicate = (pairId: string) =>
  request<{ decision_id: string; kept_work_id: string; merged_work_id: string }>(
    `/duplicates/${pairId}/merge`,
    { method: "POST" },
  );

/** Two different papers: the pair leaves the queue and no rule will merge it. */
export const dismissDuplicate = (pairId: string) =>
  request<{ id: string; status: string }>(`/duplicates/${pairId}/dismiss`, { method: "POST" });

/** What a merge rule would merge now, and why it skips the rest. Changes nothing. */
export const previewMergeRule = (rule: MergeRule) =>
  request<MergePlan>("/duplicates/merge-rule/preview", {
    method: "POST",
    body: JSON.stringify(rule),
  });

/**
 * Merge up to 200 groups the rule accepts; repeat until `remaining_groups` is 0,
 * passing back the `step_id` the first call returned so the history shows one step.
 */
export const applyMergeRule = (rule: MergeRule, stepId: string | null = null) =>
  request<{
    groups_merged: number;
    works_merged: number;
    remaining_groups: number;
    step_id?: string;
  }>(`/duplicates/merge-rule/apply${queryString({ step_id: stepId })}`, {
    method: "POST",
    body: JSON.stringify(rule),
  });

/** What applying an approved scope version's rules would change now. Changes nothing. */
export const previewScopeRules = (scopeVersionId: string) =>
  request<ScopeRulesPlan>(`/scopes/${scopeVersionId}/rules/preview`, { method: "POST" });

/** Make up to 1,000 of the preview's changes; repeat until `remaining` is 0. */
export const applyScopeRules = (scopeVersionId: string, stepId: string | null = null) =>
  request<{ applied: number; remaining: number; step_id: string }>(
    `/scopes/${scopeVersionId}/rules/apply`,
    { method: "POST", body: JSON.stringify({ step_id: stepId }) },
  );

/** What deleting this version's whole scope would remove, and what stops it. Changes nothing. */
export const previewScopeDeletion = (scopeVersionId: string) =>
  request<ScopeDeletionPlan>(`/scopes/${scopeVersionId}/deletion`);

/** Delete every version of the scope, its runs and the papers only it found. */
export const deleteScope = (scopeVersionId: string, name: string) =>
  request<{ deleted: ScopeDeletionPlan["counts"] }>(`/scopes/${scopeVersionId}/delete-scope`, {
    method: "POST",
    body: JSON.stringify({ name }),
  });

/** The review history, newest first; with `scope_version_id`, only that version's steps. */
export const listReviewHistory = (
  options: PageRequest & { scope_version_id?: string | null; kind?: string | null } = {},
) => request<Page<ReviewStep>>(`/review/history${queryString({ ...options })}`);

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

/** The adjustable parse-quality limits, each format's default, and why documents degrade. */
export const getParseQuality = () => request<ParseQuality>("/parse-quality-limits");

/** New parse-quality limits; degraded documents are checked again on the next fetch press. */
export const setParseLimits = (limits: ParseLimits) =>
  request<ParseQuality>("/parse-quality-limits", { method: "PUT", body: JSON.stringify(limits) });

/**
 * Plan every included paper that has no plan, put waiting fetches that still
 * have an automatic route back in the queue, and check degraded documents
 * against the parse-quality limits in force.
 */
export const fetchIncluded = () =>
  request<{
    requeued: number;
    kept_for_a_person: number;
    already_tried: number;
    planning_task_id: string;
    degraded_promoted: number;
    still_degraded: number;
  }>(
    "/acquisition-plans/fetch-included",
    { method: "POST" },
  );

/** One page of acquisition plans, and how many plans are in each status. */
export const listPlans = (options: PageRequest & { status?: string } = {}) =>
  request<Page<AcquisitionPlan> & { status_counts: Record<string, number> }>(
    `/acquisition-plans${queryString({ ...options })}`,
  );

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

/** One file's fate in a bulk import. */
export interface BulkImportItem {
  filename: string;
  outcome: "imported" | "blocked" | "unmatched" | "ambiguous" | "rejected" | "matched";
  identifiers: string[];
  plan_id: string | null;
  work_id: string | null;
  asset_id: string | null;
  parse_task_id: string | null;
  detail: string | null;
}

/**
 * Match a batch of PDFs to waiting acquisitions and import the clear matches.
 * With `dryRun` nothing is stored: the answer says what would happen.
 */
export async function bulkImport(
  files: File[],
  form: { accessBasis: string; attestation: string; dryRun: boolean },
): Promise<{ dry_run: boolean; counts: Record<string, number>; items: BulkImportItem[] }> {
  const body = new FormData();
  for (const file of files) body.append("files", file);
  body.set("access_basis", form.accessBasis);
  body.set("attestation", form.attestation);
  body.set("dry_run", form.dryRun ? "true" : "false");
  const response = await fetch(appPath("/api/backend/bulk-imports"), { method: "POST", body });
  if (!response.ok) {
    const problem = (await response.json().catch(() => ({}))) as Partial<ProblemDetails>;
    throw new ApiError({
      type: "about:blank",
      title: "bulk import failed",
      status: response.status,
      detail: problem.detail ?? "the batch was not accepted",
      error_class: problem.error_class ?? "internal",
    });
  }
  return response.json();
}

// ── review ────────────────────────────────────────────────────────────────
/** Values waiting for review, one page at a time. */
export const listReviewQueue = (includeInvalid = false, options: PageRequest = {}) =>
  request<Page<ReviewItem>>(
    `/claims${queryString({ include_invalid: includeInvalid ? "true" : "false", ...options })}`,
  );

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
/** Delete a draft or rejected release that nothing points at. */
export const deleteRelease = (releaseId: string) =>
  request<void>(`/corpus-releases/${releaseId}`, { method: "DELETE" });

export type ReleaseAlias = { alias: string; release_id: string | null; previous_release_id: string | null };

/** Releases, newest first, one page at a time; every alias with each page. */
export const listReleases = (options: PageRequest = {}) =>
  request<Page<Release> & { aliases: ReleaseAlias[] }>(`/corpus-releases${queryString({ ...options })}`);

/** Every release, for a menu that must offer all of them. Releases are few. */
export async function listAllReleases(): Promise<Release[]> {
  const releases: Release[] = [];
  let after: string | null = null;
  do {
    const page: Page<Release> = await listReleases({ after, limit: 200 });
    releases.push(...page.items);
    after = page.next_cursor;
  } while (after);
  return releases;
}

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

/** Publish. `overrideReason` lets an admin publish past a required benchmark. */
export const publishRelease = (releaseId: string, alias = "current", overrideReason?: string) =>
  request<{ alias: string; release_id: string; previous_release_id: string | null }>(
    `/corpus-releases/${releaseId}/publish?alias=${encodeURIComponent(alias)}`,
    {
      method: "POST",
      body: overrideReason ? JSON.stringify({ override_reason: overrideReason }) : undefined,
    },
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

/**
 * Where to download an export: the whole package as a zip, or one file of it.
 * A plain link, so the browser streams the file to disk with the session cookie.
 */
export function exportDownloadUrl(releaseId: string, file?: string): string {
  const query = file ? `?file=${encodeURIComponent(file)}` : "";
  return appPath(`/api/backend/exports/${releaseId}/download${query}`);
}

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

export const taskSnapshot = () => request<TaskSnapshot>("/operations/tasks");

/** Put a dead-lettered task back in the queue. */
export const retryTask = (taskId: string) =>
  request<{ task_id: string; state: string }>(`/operations/tasks/${taskId}/retry`, {
    method: "POST",
  });

/** One page of a task queue, oldest first; `stage` narrows it to one stage. */
export const taskQueue = (
  queue: TaskQueueName,
  options: PageRequest & { stage?: string | null; q?: string | null } = {},
) =>
  request<Page<QueueTask>>(`/operations/queue${queryString({ queue, ...options })}`);

/**
 * One page of the audit trail, newest first. `q` keeps events holding every word
 * in their action, subject, reason or details; `action` keeps one action.
 */
export const listAuditEvents = (options: PageRequest & { action?: string | null; q?: string | null } = {}) =>
  request<Page<AuditEvent> & { action_counts: Record<string, number> }>(
    `/audit-events${queryString({ ...options })}`,
  );

// ── members and invitations ───────────────────────────────────────────────
export const listMembers = () =>
  request<{ items: Member[]; token_limit_default: number; chat_admin_access: boolean }>(
    "/members",
  );

/** `token_limit: null` returns the member to the installation default. */
export const updateMember = (
  userId: string,
  change: { role?: string; status?: string; token_limit?: number | null },
) => request<Member>(`/members/${userId}`, { method: "PATCH", body: JSON.stringify(change) });

/** A member's conversations. Every call is recorded in the audit log. */
export const listMemberSessions = (userId: string, options: PageRequest = {}) =>
  request<Page<MemberSession>>(`/members/${userId}/sessions${queryString({ ...options })}`);

export const getMemberSession = (userId: string, sessionId: string) =>
  request<MemberSession & { messages: ChatMessage[] }>(`/members/${userId}/sessions/${sessionId}`);

/** Sign a member out everywhere without changing their access. */
export const revokeMemberSessions = (userId: string) =>
  request<{ user_id: string }>(`/members/${userId}/revoke-sessions`, { method: "POST" });

export const listInvitations = () =>
  request<{ items: Invitation[] }>("/invitations").then((body) => body.items);

/** Create an invitation. The token in the answer is shown once and never again. */
export const createInvitation = (payload: { email: string; role: string; ttl_hours?: number }) =>
  request<{ id: string; email: string; role: string; expires_at: string; token: string }>(
    "/invitations",
    { method: "POST", body: JSON.stringify(payload) },
  );

export const revokeInvitation = (invitationId: string) =>
  request<{ id: string; state: string }>(`/invitations/${invitationId}/revoke`, {
    method: "POST",
  });

/** Remove a revoked or expired invitation from the list. */
export const deleteInvitation = (invitationId: string) =>
  request<void>(`/invitations/${invitationId}`, { method: "DELETE" });

/** The token travels in a POST body, never in a URL an access log would keep. */
export const previewInvitation = (token: string) =>
  request<InvitationPreview>("/invitations/preview", {
    method: "POST",
    body: JSON.stringify({ token }),
  });

/** Redeem an invitation. The route handler sets the session cookie, as sign-in does. */
export async function acceptInvitation(payload: {
  token: string;
  password: string;
  full_name?: string;
}): Promise<void> {
  const response = await fetch(appPath("/api/auth/accept-invitation"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const problem = (await response.json().catch(() => ({}))) as Partial<ProblemDetails>;
    throw new ApiError({
      type: "about:blank",
      title: "invitation not accepted",
      status: response.status,
      detail: problem.detail ?? "the invitation could not be accepted",
      error_class: problem.error_class ?? "internal",
    });
  }
}

/** A link to accept an invitation. The token is a fragment: browsers never send it. */
export function invitationLink(token: string, origin = window.location.origin): string {
  return `${origin}${appPath("/invite")}#token=${encodeURIComponent(token)}`;
}

// ── evaluation ────────────────────────────────────────────────────────────
export const listBenchmarks = () =>
  request<{ items: Benchmark[] }>("/evaluation/benchmarks").then((body) => body.items);

/** Register a benchmark file. Identical content registered again is not an error. */
export const registerBenchmark = (definition: unknown) =>
  request<Benchmark & { created: boolean }>("/evaluation/benchmarks", {
    method: "POST",
    body: JSON.stringify(definition),
  });

export const getBenchmark = (benchmarkId: string, version: string) =>
  request<Benchmark>(
    `/evaluation/benchmarks/${encodeURIComponent(benchmarkId)}/${encodeURIComponent(version)}`,
  );

/** Delete a benchmark version no run used and no release gate requires. */
export const deleteBenchmark = (benchmarkId: string, version: string) =>
  request<void>(
    `/evaluation/benchmarks/${encodeURIComponent(benchmarkId)}/${encodeURIComponent(version)}`,
    { method: "DELETE" },
  );

/** Evaluation runs, newest first, one page at a time; `release_id` narrows to one release. */
export const listEvaluationRuns = (options: PageRequest & { release_id?: string | null } = {}) =>
  request<Page<EvaluationRun>>(`/evaluation/runs${queryString({ ...options })}`);

/** Queue a run. It comes back `queued`; the evaluation worker completes it. */
export const requestEvaluationRun = (payload: {
  benchmark_id: string;
  version: string;
  release_id: string;
}) =>
  request<EvaluationRun>("/evaluation/runs", { method: "POST", body: JSON.stringify(payload) });

export const getEvaluationRun = (runId: string) =>
  request<EvaluationRun>(`/evaluation/runs/${runId}`);

export const cancelEvaluationRun = (runId: string) =>
  request<EvaluationRun>(`/evaluation/runs/${runId}/cancel`, { method: "POST" });

/** Delete a failed or cancelled run that no release or reviewer used. */
export const deleteEvaluationRun = (runId: string) =>
  request<void>(`/evaluation/runs/${runId}`, { method: "DELETE" });

export const compareEvaluationRuns = (baseId: string, candidateId: string) =>
  request<RunComparison>(
    `/evaluation/compare?base=${encodeURIComponent(baseId)}&candidate=${encodeURIComponent(candidateId)}`,
  );

/** The benchmarks publishing requires; with a release, whether it passes each. */
export const listReleaseGates = (releaseId?: string) =>
  request<{ items: ReleaseGate[]; status?: GateStatus[] }>(
    `/evaluation/release-gates${releaseId ? `?release_id=${encodeURIComponent(releaseId)}` : ""}`,
  );

/** Require a benchmark version for publishing; `version: null` stops requiring it. */
export const setReleaseGate = (benchmarkId: string, version: string | null) =>
  request<{ items: ReleaseGate[] }>("/evaluation/release-gates", {
    method: "PUT",
    body: JSON.stringify({ benchmark_id: benchmarkId, version }),
  });

export const listEvaluationReviewers = () =>
  request<{ items: Reviewer[] }>("/evaluation/reviewers").then((body) => body.items);

export const assignEvaluationReviews = (
  runId: string,
  payload: { reviewer_ids: string[]; sample_size: number },
) =>
  request<{ items: AnswerReview[] }>(`/evaluation/runs/${runId}/reviews`, {
    method: "POST",
    body: JSON.stringify(payload),
  });

export const listRunReviews = (runId: string) =>
  request<RunReviewSummary>(`/evaluation/runs/${runId}/reviews`);

/** Your answer reviews, unfinished first, one page at a time. */
export const listMyAnswerReviews = (options: PageRequest = {}) =>
  request<Page<AnswerReview> & { to_do: number; scores: string[]; flags: string[] }>(
    `/evaluation/reviews/mine${queryString({ ...options })}`,
  );

export const submitAnswerReview = (
  reviewId: string,
  payload: {
    scores: Record<string, number>;
    flags: string[];
    reviewer_confidence: number | null;
    comment: string | null;
    suggested_answer: string | null;
  },
) =>
  request<AnswerReview>(`/evaluation/reviews/${reviewId}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });

// ── datasheets ────────────────────────────────────────────────────────────
export const listExtractionTemplates = () =>
  request<{ items: ExtractionTemplate[] }>("/extraction/templates").then((body) => body.items);

/** Register a template version from YAML text or a JSON object. It starts as a draft. */
export const registerExtractionTemplate = (source: { yaml: string } | { definition: unknown }) =>
  request<ExtractionTemplate & { created: boolean }>("/extraction/templates", {
    method: "POST",
    body: JSON.stringify(source),
  });

/** Make a version the one extraction uses. */
export const approveExtractionTemplate = (templateId: string, version: string) =>
  request<ExtractionTemplate>(
    `/extraction/templates/${encodeURIComponent(templateId)}/${encodeURIComponent(version)}/approve`,
    { method: "POST" },
  );

/** Delete a draft version that no run and no extracted value used. */
export const deleteExtractionTemplate = (templateId: string, version: string) =>
  request<void>(
    `/extraction/templates/${encodeURIComponent(templateId)}/${encodeURIComponent(version)}`,
    { method: "DELETE" },
  );

const templateQuery = (template?: string) =>
  template ? `?template=${encodeURIComponent(template)}` : "";

export const estimateExtraction = (template?: string) =>
  request<ExtractionEstimate>(`/extraction/estimate${templateQuery(template)}`);

export const startExtractionRun = (payload: { template?: string; rerun?: boolean }) =>
  request<{ run_id: string; template: string; tasks: number; rerun: boolean }>(
    "/extraction/runs",
    { method: "POST", body: JSON.stringify(payload) },
  );

/** Extraction runs, newest first, one page at a time. */
export const listExtractionRuns = (options: PageRequest = {}) =>
  request<Page<ExtractionRun>>(`/extraction/runs${queryString({ ...options })}`);

export const cancelExtractionRun = (runId: string) =>
  request<{ run_id: string; cancelled_tasks: number }>(`/extraction/runs/${runId}/cancel`, {
    method: "POST",
  });

/** Delete a run in which every batch failed or was cancelled. */
export const deleteExtractionRun = (runId: string) =>
  request<void>(`/extraction/runs/${runId}`, { method: "DELETE" });

/** One page of the working sheet's rows; `q` searches every row on the server. */
export const workingSheet = (template?: string, options: PageRequest & { q?: string | null } = {}) =>
  request<WorkingSheet>(`/extraction/rows${queryString({ template, ...options })}`);

/** A plain link: the browser downloads the CSV with the session cookie. */
export function workingSheetCsvUrl(shape: "wide" | "long", template?: string): string {
  const params = new URLSearchParams({ shape });
  if (template) params.set("template", template);
  return appPath(`/api/backend/extraction/rows.csv?${params.toString()}`);
}

// ── scope query lookup ────────────────────────────────────────────────────
export interface OrganismSeed {
  taxid: number;
  scientific_name: string;
  rank: string | null;
  synonyms: string[];
  common_names: string[];
  lineage: string[];
  search_terms: string[];
}

export interface ProductSeed {
  cid: number;
  preferred_name: string;
  synonyms: string[];
  molecular_formula: string | null;
  inchikey: string | null;
  search_terms: string[];
}

export const lookupOrganism = (params: { q?: string; taxid?: number; resolve?: boolean }) => {
  const query = new URLSearchParams();
  if (params.q) query.set("q", params.q);
  if (params.taxid) query.set("taxid", String(params.taxid));
  if (params.resolve) query.set("resolve", "true");
  return request<{
    suggestions: { taxid: number; scientific_name: string; rank: string | null; common_name: string | null }[];
    seed: OrganismSeed | null;
  }>(`/lookup/organism?${query.toString()}`);
};

export const lookupProduct = (params: { q?: string; cid?: number; resolve?: boolean }) => {
  const query = new URLSearchParams();
  if (params.q) query.set("q", params.q);
  if (params.cid) query.set("cid", String(params.cid));
  if (params.resolve) query.set("resolve", "true");
  return request<{ suggestions: { name: string; cid: number | null }[]; seed: ProductSeed | null }>(
    `/lookup/product?${query.toString()}`,
  );
};

/** Each source's query for the chosen names, in that source's own dialect. */
export const buildSourceQueries = (organismTerms: string[], productTerms: string[]) =>
  request<{ queries: Record<string, string> }>("/lookup/queries", {
    method: "POST",
    body: JSON.stringify({ organism_terms: organismTerms, product_terms: productTerms }),
  }).then((body) => body.queries);
