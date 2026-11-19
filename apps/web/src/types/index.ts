/** Types mirroring the API's response shapes. Generated clients would drift
 *  silently; these are checked against the OpenAPI schema in CI. */

export interface Workspace {
  id: string;
  slug: string;
  name: string;
}

export interface Me {
  user_id: string;
  email: string;
  full_name: string | null;
  workspace: Workspace;
  role: string | null;
  capabilities: string[];
  /** Whether admins of this workspace may read members' conversations. */
  chat_visible_to_admins?: boolean;
}

/** A member's own token allowance. */
export interface Allowance {
  token_limit: number;
  used_tokens: number;
  remaining_tokens: number;
  limit_reached: boolean;
  custom_limit: boolean;
}

export interface MemberUsage {
  sessions: number;
  questions: number;
  answers: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  last_active_at: string | null;
  avg_latency_ms: number | null;
}

export interface ChatSession {
  id: string;
  title: string | null;
  pinned_release_id: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export type CitationStatus =
  | "valid"
  | "not_supplied"
  | "superseded"
  | "rights_withdrawn"
  | "legacy_unverified";

export interface Citation {
  handle: string;
  status: CitationStatus;
  work_id: string | null;
  display_label: string | null;
  evidence_span_id: string | null;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string | null;
  release_id: string | null;
  citations: Citation[];
  citation_validation_passed: boolean;
  unsupported_handles: string[];
  latency_ms: number;
}

export interface SourceBlock {
  handle: string;
  title: string | null;
  year: number | null;
  source_label: string | null;
  locator: string | null;
  display_restricted: boolean;
  snippet: string;
}

/** Streamed answer events. `sources` arrives before any prose, so the interface
 *  can show what the answer is grounded in while it is still being written. */
export type AskEvent =
  | { type: "sources"; data: { release_id: string; classification: string; sources: SourceBlock[]; claims: unknown[] } }
  | { type: "token"; data: string }
  | {
      type: "done";
      data: {
        message_id: string;
        citations: Citation[];
        unsupported_handles: string[];
        citation_validation_passed: boolean;
        release_id: string;
        latency_ms: number;
      };
    }
  | { type: "error"; data: { detail: string; error_class: string } };

export interface Candidate {
  work_id: string;
  title: string | null;
  year: number | null;
  journal: string | null;
  work_kind: string;
  identity_status: string;
  relevance: string;
  retraction_status: string;
  identifiers: { scheme: string; value: string; status: string }[];
  provenance: Record<string, string[]>;
}

export interface AcquisitionPlan {
  plan_id: string;
  work_id: string;
  status: string;
  routes: string[];
  manifestation_ids: string[];
  attempts: {
    route: string;
    connector_id: string;
    outcome: string | null;
    http_status: number | null;
    note: string | null;
  }[];
}

export interface ReviewItem {
  claim_id: string;
  revision_id: string;
  revision_number: number;
  work_id: string;
  predicate: string;
  subject: string | null;
  value: Record<string, unknown>;
  validation_status: string;
  validation_issues: string[];
  abstention_reason: string;
  review_status: string;
  proposal_source: string;
  evidence: {
    span_id: string;
    node_id: string;
    quote_preview: string;
    locator: Record<string, unknown>;
    normalisation_version: string;
  }[];
  previous_revisions: string[];
  conflicting_revisions: string[];
}

export interface Release {
  id: string;
  name: string;
  semantic_version: string;
  status: string;
  manifest_sha256: string | null;
  published_at: string | null;
  gate_results: Record<string, boolean>;
  projection_ready_profiles: string[];
  parent_release_id: string | null;
}

export interface CorpusStats {
  release_id: string;
  release_name: string;
  release_version: string;
  published_at: string | null;
  works: number;
  renditions: number;
  claims: number;
  chunks: number;
  //: Null for members who may not manage releases: how much is withheld is itself
  //: a measure of material they cannot open.
  withheld_by_policy: number | null;
}

export interface ProblemDetails {
  type: string;
  title: string;
  status: number;
  detail: string;
  error_class: string;
  /** Structured detail, such as `problems` for an invalid benchmark file. */
  context?: Record<string, unknown>;
}

/** A registered benchmark version. Immutable: a change is a new version. */
export interface Benchmark {
  benchmark_id: string;
  version: string;
  key: string;
  kind: string;
  description: string;
  sensitivity: string;
  blinded: boolean;
  checksum: string;
  cases: number;
  probes: number;
  thresholds: Record<string, number>;
  registered_by: string | null;
  created_at: string | null;
  definition?: Record<string, unknown>;
}

/** One evaluation of a benchmark against a release. `passed` is the API's verdict. */
export interface EvaluationRun {
  id: string;
  benchmark: string;
  benchmark_checksum: string;
  release_id: string | null;
  kind: string | null;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  passed: boolean;
  metrics: Record<string, number>;
  gates: Record<string, boolean>;
  failures: string[];
  task_id: string | null;
  requested_by: string | null;
  error_detail: string | null;
  created_at: string | null;
  completed_at: string | null;
  per_case?: Record<string, unknown>[];
}

/** A workspace membership with its account. */
export interface Member {
  user_id: string;
  email: string;
  full_name: string | null;
  role: string;
  status: "active" | "suspended" | "invited" | "expired";
  extra_capabilities: string[];
  capabilities: string[];
  accepted_at: string | null;
  last_login_at: string | null;
  account_active: boolean;
  token_limit: number;
  custom_token_limit: boolean;
  usage: MemberUsage;
  limit_reached: boolean;
}

/** A member's conversation as an admin sees it. `deleted` ones the member removed. */
export interface MemberSession extends ChatSession {
  deleted: boolean;
}

export interface Invitation {
  id: string;
  email: string;
  role: string;
  state: "open" | "accepted" | "revoked" | "expired";
  created_at: string;
  expires_at: string;
  accepted_at: string | null;
  invited_by: string;
}

/** What an invitation offers, shown before it is accepted. */
export interface InvitationPreview {
  email: string;
  role: string;
  workspace: { slug: string; name: string };
  expires_at: string;
  has_account: boolean;
}

export interface TaskSnapshot {
  counts: Record<string, number>;
  oldest_pending_by_stage: Record<string, string>;
  awaiting_user: { task_id: string; stage: string; action: string | null }[];
  dead_letters: { task_id: string; stage: string; error: string | null }[];
}

export interface AuditEvent {
  id: string;
  occurred_at: string;
  actor_type: string;
  action: string;
  subject_type: string;
  subject_id: string | null;
  reason_code: string | null;
  metadata: Record<string, unknown>;
  integrity_hash: string;
}

/** One immutable version of a collection scope. */
export interface ScopeVersion {
  id: string;
  scope_id: string;
  version: number;
  name: string;
  approval_state: string;
  connectors: string[];
}

export interface ScopeGateReport {
  passed: boolean;
  blockers: string[];
  compiled_connectors: string[];
  gold_seed_recall: number | null;
  estimated_requests: number | null;
}

export interface DiscoveryRun {
  id: string;
  status: string;
  started_at: string | null;
  frozen_at: string | null;
  degraded_reason: string | null;
  connectors: {
    connector_id: string;
    required: boolean;
    outcome: string | null;
    records_seen: number;
    declared_total: number | null;
    accepted_degraded: boolean;
  }[];
  metrics: Record<string, unknown>;
}

export interface ReleaseGate {
  benchmark_id: string;
  version: string;
  key: string;
  set_by: string | null;
  set_at: string | null;
}

export interface GateStatus {
  benchmark_id: string;
  version: string;
  key: string;
  passed: boolean;
  run_id: string | null;
  run_status: string | null;
}

export interface RunComparison {
  base: { id: string; benchmark: string; release_id: string };
  candidate: { id: string; benchmark: string; release_id: string };
  same_version: boolean;
  metrics: { name: string; base: number | null; candidate: number | null; delta: number | null }[];
  gates: { name: string; base: boolean | null; candidate: boolean | null }[];
  changed_cases: {
    case_id: string;
    only_in: "base" | "candidate" | null;
    changes: Record<string, { base: unknown; candidate: unknown }>;
  }[];
  unchanged_cases: number;
}

export interface Reviewer {
  user_id: string;
  email: string;
  full_name: string | null;
  role: string;
}

/** One case of a generation run, assigned to a person to grade. */
export interface AnswerReview {
  id: string;
  run_id: string;
  case_id: string;
  assigned_to: string;
  assigned_by: string | null;
  status: "assigned" | "submitted";
  scores: Record<string, number>;
  flags: string[];
  reviewer_confidence: number | null;
  comment: string | null;
  suggested_answer: string | null;
  created_at: string | null;
  submitted_at: string | null;
  case?: Record<string, unknown> | null;
}

export interface RunReviewSummary {
  run_id: string;
  assigned: number;
  submitted: number;
  mean_scores: Record<string, number | null>;
  flag_counts: Record<string, number>;
  items: AnswerReview[];
}
