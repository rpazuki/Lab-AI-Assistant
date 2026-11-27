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
  /** From the paper's abstract: its full text could not be fetched. */
  abstract_only?: boolean;
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

/** One keyset page of a long list. `next_cursor` is null on the last page. */
export interface Page<T> {
  items: T[];
  next_cursor: string | null;
  /** Rows that match, across every page. */
  total: number;
}

/** Enough of a paper to recognise it: what the review queues and previews show. */
export interface PaperSummary {
  work_id: string;
  title: string | null;
  year: number | null;
  journal: string | null;
  resource_kind: string | null;
  first_author: string | null;
  sources: string[];
  /** Where two papers are compared for a merge: their DOIs, empty when none. */
  dois?: string[];
  relevance?: string;
  identity_status?: string;
}

/** An open pair in the duplicate-review queue, with both papers. */
export interface DuplicateProposal {
  id: string;
  left_work_id: string;
  right_work_id: string;
  similarity: number;
  year_gap: number | null;
  first_author_match: boolean | null;
  left: PaperSummary;
  right: PaperSummary;
}

/** What the candidate list shows and what a bulk decision applies to. Every condition must hold. */
export interface CandidateFilters {
  relevance?: string;
  identity_status?: string;
  /** Words that must all appear in the title, comma-separated. */
  title?: string;
  /** Words none of which may appear in the title. */
  exclude?: string;
  /** Subject terms, any one of which must appear. */
  subject?: string;
  year_from?: number | null;
  year_to?: number | null;
  kind?: string;
  journal?: string;
  source?: string;
  has_abstract?: boolean | null;
  /** Languages, any one of which (two-letter codes, or "unknown"), comma-separated. */
  language?: string;
  /** Languages none of which, comma-separated; "unknown" leaves out papers with none. */
  exclude_language?: string;
  /** Match the title words in the abstract and subject terms too. */
  in_abstract?: boolean;
}

export interface CandidateFacets {
  /** Papers per language code; "unknown" for those with none recorded or detected. */
  languages?: Record<string, number>;
  kinds: Record<string, number>;
  sources: Record<string, number>;
  year_min: number | null;
  year_max: number | null;
  /** Works the filter index covers; below `works` while it is still being built. */
  summarised: number;
  works: number;
}

export interface BulkRelevanceResult {
  matched: number;
  sample: PaperSummary[];
  applied: number;
  remaining: number;
  /** Set by an apply: pass it back so every round is one history step. */
  step_id?: string;
}

/** A paper whose identifiers the sources disagree on, and the other paper(s) involved. */
export interface IdentityConflict extends PaperSummary {
  identifiers: { scheme: string; value: string }[];
  with: (PaperSummary & {
    /** two_papers_matched_one_record, or identifier_held_by_another_paper. */
    reason: string;
    /** For an identifier held by another paper: which one, as "doi:10.…". */
    identifier: string | null;
  })[];
}

/** A page of conflicts, and how many (paper, other paper) pairs the search found in all. */
export interface IdentityConflictPage extends Page<IdentityConflict> {
  pairs: number;
}

export interface MergeRule {
  min_similarity: number;
  require_same_year: boolean;
  skip_different_authors: boolean;
  require_known_authors: boolean;
  skip_different_dois: boolean;
  max_pairs_per_work: number;
  max_group_size: number;
  /** "Merge these anyway": only the pairs skipped for this reason, with its check off. */
  override?: string | null;
}

export interface MergePlan {
  considered: number;
  accepted_pairs: number;
  groups: number;
  works_merged: number;
  skipped: {
    reason: string;
    description: string;
    count: number;
    /** Whether a steward may merge these anyway. */
    overridable: boolean;
    examples: { similarity: number; left: PaperSummary; right: PaperSummary }[];
  }[];
  sample_groups: { similarity: number; papers: PaperSummary[] }[];
}

export interface Candidate {
  work_id: string;
  title: string | null;
  year: number | null;
  journal: string | null;
  /** For a merged paper, the paper it was merged into. */
  merged_into_work_id?: string | null;
  work_kind: string;
  /** Two-letter code, recorded by a source or (``language_detected``) read from the text. */
  language?: string | null;
  language_detected?: boolean;
  identity_status: string;
  relevance: string;
  retraction_status: string;
  identifiers: { scheme: string; value: string; status: string }[];
  provenance: Record<string, string[]>;
}

/** The collection before publication, stage by stage (Collection progress). */
export interface CollectionProgress {
  funnel: { stage: string; label: string; count: number }[];
  discovery: {
    runs: number;
    by_source: Record<string, number>;
    merged: number;
    open_duplicate_pairs: number;
    identity_conflicts: number;
  };
  screening: {
    relevance: Record<string, number>;
    decided_by_person: number;
    with_abstract: number;
    included_with_abstract: number;
    found: number;
    included: number;
  };
  acquisition: {
    plans: Record<string, number>;
    waiting_reasons: Record<string, number>;
    failed_reasons: Record<string, number>;
    identity: Record<string, number>;
    preprints: number;
  };
  parsing: { by_status: Record<string, number>; degraded_by_reason: Record<string, number> };
  extraction: {
    papers_with_values: number;
    values: number;
    waiting_review: number;
    tasks: Record<string, number>;
  };
  /** How each publisher site answered the current fetcher, refusals first. */
  sites: {
    rows: SiteRow[];
    total: number;
    resting: number;
    seconds_between_requests: number;
  };
  daily: { day: string; found: number; fetched: number; parsed: number }[];
}

export interface SiteRow {
  host: string;
  fetched: number;
  /** Rate limits and bot checks: the site asked us to slow down. */
  slowed_down: number;
  blocked: number;
  sign_in: number;
  other: number;
  /** While the site rests after pushing back; null when it may be asked. */
  resting_until: string | null;
  strikes: number;
}

/** Abstracts in place of full texts nobody could fetch (a steward's recorded choice). */
export interface AbstractFallback {
  enabled: boolean;
  set_at: string | null;
  set_by: string | null;
  /** Plans whose abstract stands in for the paper now. */
  partial: number;
  /** Papers an abstract could stand in for: included, no file, an abstract on record. */
  eligible: number;
}

/** The two parse-quality limits a steward can adjust; null keeps a format's default. */
export interface ParseLimits {
  max_duplicate_paragraph_ratio: number | null;
  require_reference_separation: boolean | null;
}

export interface ParseQuality {
  limits: ParseLimits;
  /** Each document format's own defaults, used where a limit is not set. */
  defaults: Record<string, { profile: string } & { max_duplicate_paragraph_ratio: number; require_reference_separation: boolean }>;
  set_at: string | null;
  degraded: number;
  degraded_by_reason: Record<string, number>;
}

export interface AcquisitionPlan {
  plan_id: string;
  work_id: string;
  /** The paper the plan fetches: title, year, journal. */
  paper?: PaperSummary;
  /** Its DOI, PMID and PMCID. */
  identifiers?: { scheme: string; value: string }[];
  /** The stored file: how it was tied to the paper, and whether it is a preprint. */
  file?: { identity_verified: boolean; identity_basis: string | null; version: string | null } | null;
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

/** A paper a release adds, changes or drops against the release it follows. */
export interface ReleaseChangePaper {
  work_id: string;
  title: string | null;
  year: number | null;
  dois: string[];
  /** Dropped papers only: why, as the paper shows it now. */
  why?: "merged" | "not_included" | "no_usable_text" | "deleted";
}

/** What a release adds, changes and drops against its parent release. */
export interface ReleaseChanges {
  release_id: string;
  /** The release it follows; none for a first, full build. */
  parent: { id: string; name: string; semantic_version: string } | null;
  items: number;
  added: number;
  changed: number;
  removed: number;
  unchanged: number;
  /** At most 100 of each, and only papers you may reach. */
  papers: { added: ReleaseChangePaper[]; changed: ReleaseChangePaper[]; removed: ReleaseChangePaper[] };
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
  /** Runs measured against this version; one with runs cannot be deleted. */
  runs?: number;
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
  /** What their role is called: a built-in one, or one this workspace defined. */
  role: string;
  /** Where their capabilities come from. The same as `role` unless it is a defined one. */
  base_role: string;
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
  /** A link to open, or a temporary password to type once at sign-in. */
  kind: InvitationKind;
  created_at: string;
  expires_at: string;
  accepted_at: string | null;
  invited_by: string;
}

export type InvitationKind = "link" | "temporary_password";

/** The workspace's wording of each invitation email; null where it uses the product's. */
export interface InvitationEmailSetting {
  templates: Record<InvitationKind, { subject: string; body: string } | null>;
  set_at: string | null;
  set_by: string | null;
}

/** An invitation as created: its token is a link's, or the temporary password. Shown once. */
export interface CreatedInvitation {
  id: string;
  email: string;
  role: string;
  kind: InvitationKind;
  expires_at: string;
  token: string;
}

/** What an invitation offers, shown before it is accepted. */
export interface InvitationPreview {
  email: string;
  role: string;
  workspace: { slug: string; name: string };
  expires_at: string;
  has_account: boolean;
}

/** The task lists the operations page pages through, oldest first. */
export type TaskQueueName = "waiting" | "awaiting_user" | "dead_letter";

export interface QueueTask {
  task_id: string;
  stage: string;
  state: string;
  created_at: string | null;
  /** When it ended: for a dead letter, when it ran out of retries. */
  completed_at?: string | null;
  attempt: number;
  action: string | null;
  error: string | null;
}

/** What the workers are doing now, and what limits how many tasks run at once. */
export interface RunningWork {
  running: {
    task_id: string;
    stage: string;
    worker_class: string;
    /** The worker holding it, as "<host>/<class>". */
    worker: string | null;
    /** The task in words: "Fetching the full text of “…”". */
    doing: string;
    paper: { work_id: string; title: string | null; year: number | null; dois: string[] } | null;
    attempt: number;
    max_attempts: number;
    first_started_at: string | null;
    heartbeat_at: string | null;
    lease_expires_at: string | null;
    /** Its worker stopped renewing the lease: the next free worker takes it over. */
    stalled: boolean;
  }[];
  stages: { stage: string; worker_class: string; running: number; limit: number; ready: number }[];
  busy_workers: { worker: string; worker_class: string }[];
  lease_seconds: number;
  poll_seconds: number;
}

export interface TaskSnapshot {
  /** Tasks per state, over every task. */
  counts: Record<string, number>;
  oldest_pending_by_stage: Record<string, string>;
  /** Ready or leased tasks per stage, the longest-waiting stage first. */
  waiting_by_stage: { stage: string; count: number; oldest: string | null }[];
  /** The first page of each; `taskQueue` pages the rest. */
  awaiting_user: QueueTask[];
  dead_letters: QueueTask[];
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
  scientific_objective: string;
  concepts: string[];
  exclusion_terms: string[];
  relevance_policy: { include_threshold: number; exclude_threshold: number };
  intended_uses: string[];
  connector_plan: { connector_id: string; query: string; required: boolean }[];
  created_at: string | null;
  frozen_at: string | null;
}

/** What applying an approved scope version's screening rules would change. */
export interface ScopeRulesPlan {
  scope: {
    scope_version_id: string;
    name: string;
    version: number;
    concepts: string[];
    exclusion_terms: string[];
    include_threshold: number;
    exclude_threshold: number;
  };
  /** Active candidates in the collection. */
  considered: number;
  /** Decided by a person: never changed by the rules. */
  decided_by_person: number;
  /** Whether candidates decided with a filter are screened again. */
  include_filter_decisions: boolean;
  /** Of those screened again, how many a filter had decided. */
  filter_decided: number;
  /** Not yet indexed for the rules; the deploy's backfill fills these. */
  not_indexed: number;
  unchanged: number;
  changes: number;
  /** What the rules say, by relevance, over every candidate they may decide. */
  outcome: Record<string, number>;
  /** "unreviewed>excluded" → how many. */
  transitions: Record<string, number>;
  reasons: { relevance: string; reason: string; description: string; count: number }[];
  samples: Record<string, (PaperSummary & { reason: string })[]>;
  no_concepts: boolean;
}

/** What deleting a whole scope would remove, and what stops it. */
export interface ScopeDeletionPlan {
  scope_id: string;
  name: string;
  versions: { id: string; version: number; approval_state: string }[];
  counts: {
    versions: number;
    runs: number;
    /** Papers that go: found only by this scope, and those merged into them. */
    papers: number;
    /** Papers other scopes also found: they stay, without this scope's records. */
    papers_kept: number;
    decided_by_person: number;
    files: number;
    parsed_texts: number;
    values: number;
    value_reviews: number;
    draft_releases: number;
    tasks: number;
  };
  blockers: string[];
  deletable: boolean;
}

/** One role a rule or a member can name. `custom` ones were defined by an admin. */
export interface RoleOption {
  name: string;
  label: string;
  custom: boolean;
}

/**
 * A role a member can be given, with what it is for and how many hold it.
 *
 * `id` is null for the four that ship with the product: they are names in the
 * code, not rows, so there is nothing to change or remove.
 */
export interface Role {
  id: string | null;
  name: string;
  display_name: string;
  description: string;
  base_role: string;
  custom: boolean;
  members: number;
  capabilities: string[];
}

/** Who may reach a scope's papers, and the roles and people to choose from. */
export interface ScopeAccess {
  scope_id: string;
  name: string;
  rules: {
    id: string;
    subject_type: string;
    subject_role: string | null;
    subject_id: string | null;
    effect: string;
    policy_basis: string;
  }[];
  /** Roles a rule can name. Admins are left out: they reach everything anyway. */
  roles: RoleOption[];
  members: { user_id: string; role: string; reaches_everything: boolean }[];
}

/** One step in the review history. */
export interface ReviewStep {
  id: string;
  kind: string;
  scope_version_id: string | null;
  actor: string | null;
  rule: Record<string, unknown>;
  counts: Record<string, number>;
  papers: PaperSummary[];
  subject_id: string | null;
  note: string | null;
  started_at: string;
  finished_at: string | null;
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

/** One version of an extraction template. */
export interface ExtractionTemplate {
  id: string;
  template_id: string;
  version: string;
  key: string;
  title: string;
  scientific_purpose: string;
  cardinality: string;
  targets: {
    target_id: string;
    predicate: string;
    object_kind: string;
    unit_expectation: string | null;
    required: boolean;
    deterministic_metadata: boolean;
  }[];
  status: "draft" | "in_review" | "approved" | "retired";
  active: boolean;
  checksum: string;
  created_at: string | null;
  definition: Record<string, unknown>;
}

export interface ExtractionEstimate {
  template: string;
  renditions: number;
  already_extracted: number;
  to_extract: number;
  sampled: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number | null;
  model: string | null;
}

export interface ExtractionRun {
  run_id: string;
  template: string | null;
  states: Record<string, number>;
  total: number;
  finished: boolean;
  created_at: string | null;
  errors: string[];
}

export interface WorkingSheet {
  template: string | null;
  claims: number;
  review_status: Record<string, number>;
  /** The claims behind the rows on this page. */
  long_form: Record<string, string>[];
  /** One page of rows; `total` counts every row the search keeps. */
  wide: { columns: string[]; rows: Record<string, string>[]; total: number; next_cursor: string | null };
}
