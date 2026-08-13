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
}
