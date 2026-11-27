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
  AbstractFallback,
  AcquisitionPlan,
  Allowance,
  AnswerReview,
  AskEvent,
  AuditEvent,
  Benchmark,
  BulkRelevanceResult,
  CreatedInvitation,
  InvitationEmailSetting,
  Candidate,
  CandidateFacets,
  CandidateFilters,
  ChatMessage,
  ChatSession,
  CollectionProgress,
  CorpusStats,
  DiscoveryRun,
  DuplicateProposal,
  EvaluationRun,
  ExtractionEstimate,
  ExtractionRun,
  ExtractionTemplate,
  GateStatus,
  IdentityConflict,
  IdentityConflictPage,
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
  ReleaseChanges,
  ReleaseGate,
  Reviewer,
  ReviewItem,
  ReviewStep,
  Role,
  RunComparison,
  RunningWork,
  RunReviewSummary,
  ScopeAccess,
  ScopeDeletionPlan,
  ScopeGateReport,
  ScopeRulesPlan,
  ScopeVersion,
  TaskQueueName,
  TaskSnapshot,
  WorkingSheet,
} from "@/types";
import { appPath } from "@/lib/config";
import type { TaskWatch } from "@/lib/watchTask";

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

/** What a person reads when the browser got no answer at all. */
export const UNREACHABLE =
  "The server could not be reached: the connection dropped, or the app is restarting " +
  "after an update. Try again in a minute.";

/**
 * The browser's own fetch, with a network failure said in words.
 *
 * A dropped connection rejects with a bare "Failed to fetch", which says nothing
 * a person can act on. A read (GET) is tried once more after a moment, since
 * asking twice changes nothing; a change is not, because the first may have
 * arrived and been applied.
 */
async function reach(url: string, init: RequestInit): Promise<Response> {
  const method = (init.method ?? "GET").toUpperCase();
  try {
    return await fetch(url, init);
  } catch (first) {
    if (first instanceof DOMException && first.name === "AbortError") throw first;
    if (method === "GET") {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      try {
        return await fetch(url, init);
      } catch {
        // Fall through to the message below.
      }
    }
    throw new ApiError({
      type: "about:blank",
      title: "server unreachable",
      status: 0,
      detail: UNREACHABLE,
      error_class: "source_unavailable",
    });
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await reach(appPath(`/api/backend${path}`), {
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
      // Says when a temporary password was right and a new one is to be chosen.
      context: problem.context,
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

/** Run one source of an unfrozen run again, after it failed or ended degraded. */
export const retryDiscoverySource = (runId: string, connectorId: string) =>
  request<{ task_id: string; state: string; connector_id: string }>(
    `/discovery-runs/${runId}/sources/${encodeURIComponent(connectorId)}/retry`,
    { method: "POST" },
  );

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
  const { has_abstract, in_abstract, ...rest } = filters;
  return {
    ...rest,
    has_abstract: has_abstract === null || has_abstract === undefined ? null : String(has_abstract),
    in_abstract: in_abstract ? "true" : null,
  };
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
/** Which open duplicate pairs to show, and a merge rule to act on. */
export interface PairSearch {
  /** Words that must all appear in either paper's title. */
  q?: string;
  min_similarity?: number | null;
  /** Same year, different years, or either year unknown. */
  year?: "same" | "different" | "unknown" | null;
  /** First authors agree, differ, or either is unknown. */
  authors?: "agree" | "differ" | "unknown" | null;
}

function pairQuery(search: PairSearch): Record<string, string | number | null | undefined> {
  return {
    q: search.q,
    min_similarity: search.min_similarity,
    year: search.year,
    authors: search.authors,
  };
}

export const listConflicts = (options: PageRequest & PairSearch = {}) => {
  const { after, limit, ...search } = options;
  return request<{
    conflict_total: number;
    proposal_total: number;
    next_cursor: string | null;
    duplicate_proposals: DuplicateProposal[];
  }>(`/candidates/conflicts${queryString({ after, limit, ...pairQuery(search) })}`);
};

/** Papers in conflict, each with the other paper(s) involved and why. */
export const listIdentityConflicts = (options: PageRequest & { q?: string } = {}) =>
  request<IdentityConflictPage>(`/identity-conflicts${queryString({ ...options })}`);

/** Different papers: this one is resolved, its identifiers stay where they are. */
export const keepConflictSeparate = (workId: string) =>
  request<{ work_id: string; identity_status: string }>(
    `/identity-conflicts/${workId}/keep-separate`,
    { method: "POST" },
  );

/** Keep up to 200 of the conflicts the search finds separate; repeat until none remain. */
export const keepConflictsSeparate = (q?: string) =>
  request<{ resolved: number; remaining: number }>(
    `/identity-conflicts/keep-separate${queryString({ q })}`,
    { method: "POST" },
  );

/** Merge up to 200 groups of conflicts the search finds as the same paper; repeat until none remain. */
export const mergeConflictsSamePaper = (q?: string) =>
  request<{ merged: number; remaining: number }>(
    `/identity-conflicts/merge-same-paper${queryString({ q })}`,
    { method: "POST" },
  );

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
export const previewMergeRule = (rule: MergeRule, search: PairSearch = {}) =>
  request<MergePlan>(`/duplicates/merge-rule/preview${queryString(pairQuery(search))}`, {
    method: "POST",
    body: JSON.stringify(rule),
  });

/**
 * Merge up to 200 groups the rule accepts; repeat until `remaining_groups` is 0,
 * passing back the `step_id` the first call returned so the history shows one step.
 */
export const applyMergeRule = (
  rule: MergeRule,
  stepId: string | null = null,
  search: PairSearch = {},
) =>
  request<{
    groups_merged: number;
    works_merged: number;
    remaining_groups: number;
    step_id?: string;
  }>(`/duplicates/merge-rule/apply${queryString({ step_id: stepId, ...pairQuery(search) })}`, {
    method: "POST",
    body: JSON.stringify(rule),
  });

/** What applying an approved scope version's rules would change now. Changes nothing. */
export const previewScopeRules = (scopeVersionId: string, includeFilterDecisions = false) =>
  request<ScopeRulesPlan>(
    `/scopes/${scopeVersionId}/rules/preview${includeFilterDecisions ? "?include_filter_decisions=true" : ""}`,
    { method: "POST" },
  );

/** Make up to 1,000 of the preview's changes; repeat until `remaining` is 0. */
export const applyScopeRules = (
  scopeVersionId: string,
  stepId: string | null = null,
  includeFilterDecisions = false,
) =>
  request<{ applied: number; remaining: number; step_id: string }>(
    `/scopes/${scopeVersionId}/rules/apply`,
    {
      method: "POST",
      body: JSON.stringify({ step_id: stepId, include_filter_decisions: includeFilterDecisions }),
    },
  );

/**
 * Every role a member can be given: the four built in, then any this workspace
 * defined, each with how many people hold it.
 */
export const listRoles = () => request<{ items: Role[] }>("/roles");

/**
 * Define a role of this workspace's own.
 *
 * `name` is the stable key rules are written about and never changes afterwards;
 * `base_role` is where its members' capabilities come from, since a role carries
 * none of its own.
 */
export const createRole = (role: {
  name: string;
  display_name?: string;
  description?: string;
  base_role?: string;
}) =>
  request<{ id: string; name: string }>("/roles", {
    method: "POST",
    body: JSON.stringify(role),
  });

/** Correct a role's wording, or change what its members may do. */
export const updateRole = (
  roleId: string,
  changes: { display_name?: string; description?: string; base_role?: string },
) =>
  request<{ id: string; name: string }>(`/roles/${roleId}`, {
    method: "PATCH",
    body: JSON.stringify(changes),
  });

/** Remove a role nobody holds, with every rule that named it. */
export const deleteRole = (roleId: string) =>
  request<{ rules_removed: number }>(`/roles/${roleId}`, { method: "DELETE" });

/**
 * Who may reach this scope's papers in Ask, in releases and in exports.
 *
 * Access is held on the scope, not the version, so a rule written from any
 * version is the same rule. An admin reaches everything without one.
 */
export const scopeAccess = (scopeVersionId: string) =>
  request<ScopeAccess>(`/scopes/${scopeVersionId}/access`);

/** Let a role, or one person, reach every version of this scope. */
export const grantScopeAccess = (
  scopeVersionId: string,
  subject: { role?: string; user_id?: string; effect?: "allow" | "deny" },
) =>
  request<{ id: string }>(`/scopes/${scopeVersionId}/access`, {
    method: "POST",
    body: JSON.stringify(subject),
  });

/** Take one rule away. Whoever it was the only rule for stops reaching the scope. */
export const revokeScopeAccess = (scopeVersionId: string, grantId: string) =>
  request<void>(`/scopes/${scopeVersionId}/access/${grantId}`, { method: "DELETE" });

/** What deleting this version's whole scope would remove, and what stops it. Changes nothing. */
export const previewScopeDeletion = (scopeVersionId: string) =>
  request<ScopeDeletionPlan>(`/scopes/${scopeVersionId}/deletion`);

/**
 * Queue deleting every version of the scope, its runs and the papers only it found.
 *
 * Only the typed name is checked before it is queued; what goes, and what stops
 * it, are worked out inside the transaction that purges. Watch it with
 * `scopeDeletion`.
 */
export const deleteScope = (scopeVersionId: string, name: string) =>
  request<{ task_id: string; state: string }>(`/scopes/${scopeVersionId}/delete-scope`, {
    method: "POST",
    body: JSON.stringify({ name }),
  });

/** How the queued deletion is getting on, and its counts once it has settled. */
export const scopeDeletion = (taskId: string) =>
  request<TaskWatch<ScopeDeletionPlan["counts"]>>(`/scope-deletions/${taskId}`);

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

/** How far the collection has got before publication, stage by stage. */
export const collectionProgress = () => request<CollectionProgress>("/collection-progress");

/** The adjustable parse-quality limits, each format's default, and why documents degrade. */
export const getParseQuality = () => request<ParseQuality>("/parse-quality-limits");

/** Whether abstracts stand in for full texts nobody could fetch, and for how many papers. */
export const getAbstractFallback = () => request<AbstractFallback>("/abstract-fallback");

/** A steward's choice; off takes abstracts out of the next release. */
export const setAbstractFallback = (enabled: boolean) =>
  request<AbstractFallback>("/abstract-fallback", { method: "PUT", body: JSON.stringify({ enabled }) });

/** Build up to 500 abstract documents; repeat until `remaining` is 0. */
export const buildAbstractDocuments = () =>
  request<{ built: number; remaining: number; enabled: number }>("/abstract-fallback/build", {
    method: "POST",
  });

/** New parse-quality limits; degraded documents are checked again on the next fetch press. */
export const setParseLimits = (limits: ParseLimits) =>
  request<ParseQuality>("/parse-quality-limits", { method: "PUT", body: JSON.stringify(limits) });

/** What the sweep did, once it has finished. */
export interface FetchSweepCounts {
  requeued: number;
  /** Upload-only papers given the open routes: discovery now records an open licence. */
  replanned: number;
  kept_for_a_person: number;
  already_tried: number;
  degraded_promoted: number;
  still_degraded: number;
}

/**
 * Queue the sweep: plan every included paper that has no plan, put waiting
 * fetches that still have an automatic route back in the queue, and check
 * degraded documents against the parse-quality limits in force.
 *
 * It reads tens of thousands of rows, so it runs as a task rather than inside
 * this request, which used to be cut off half done by the gateway. Watch it with
 * `fetchSweep`; it carries on whether or not anyone is watching.
 */
export const fetchIncluded = () =>
  request<{ task_id: string; state: string }>("/acquisition-plans/fetch-included", {
    method: "POST",
  });

/** How the queued sweep is getting on, and its counts once it has settled. */
export const fetchSweep = (taskId: string) =>
  request<TaskWatch<FetchSweepCounts>>(`/acquisition-plans/fetch-included/${taskId}`);

/** One page of acquisition plans, and how many plans are in each status. */
export const listPlans = (
  options: PageRequest & {
    status?: string;
    /** Words each in the paper's title or one of its identifiers (a DOI). */
    q?: string;
    /** The latest attempt's outcome; "none" for never tried. */
    outcome?: string;
  } = {},
) =>
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
  const response = await reach(appPath(`/api/backend/assisted-acquisitions/${planId}`), {
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

/** What a release adds, changes and drops against the release it follows. */
export const releaseChanges = (releaseId: string) =>
  request<ReleaseChanges>(`/corpus-releases/${releaseId}/changes`);

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

/** Build an approved release's index again after its chunk-count check failed. */
export const reindexRelease = (releaseId: string) =>
  request<{ release_id: string; status: string; indexing_tasks: string[] }>(
    `/corpus-releases/${releaseId}/reindex`,
    { method: "POST" },
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

/** What each worker is doing now, and each stage's running, waiting and limit. */
export const runningWork = () => request<RunningWork>("/operations/running");

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

/**
 * Create an invitation. The token in the answer is shown once and never again:
 * a link's token, or with `temporary_password` the password to send by hand.
 */
export const createInvitation = (payload: {
  email: string;
  role: string;
  ttl_hours?: number;
  temporary_password?: boolean;
}) =>
  request<CreatedInvitation>("/invitations", { method: "POST", body: JSON.stringify(payload) });

/** The workspace's wording of the invitation emails; null for a kind using the product's. */
export const getInvitationEmail = () => request<InvitationEmailSetting>("/invitation-email");

/** Save the wording of both kinds; null for a kind restores the product's. */
export const saveInvitationEmail = (templates: InvitationEmailSetting["templates"]) =>
  request<InvitationEmailSetting>("/invitation-email", {
    method: "PUT",
    body: JSON.stringify(templates),
  });

/** A new temporary password for the same person and role; the earlier one stops working. */
export const regenerateTemporaryPassword = (invitationId: string) =>
  request<CreatedInvitation>(`/invitations/${invitationId}/regenerate`, { method: "POST" });

/** Exchange a temporary password for one's own, and arrive signed in. */
export async function setFirstPassword(payload: {
  email: string;
  temporary_password: string;
  password: string;
  full_name?: string;
}): Promise<void> {
  const response = await fetch(appPath("/api/auth/first-sign-in"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const problem = (await response.json().catch(() => ({}))) as Partial<ProblemDetails>;
    throw new ApiError({
      type: "about:blank",
      title: "password not set",
      status: response.status,
      detail: problem.detail ?? "the password could not be set",
      error_class: problem.error_class ?? "authentication_required",
      context: problem.context,
    });
  }
}

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
