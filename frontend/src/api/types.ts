export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  has_more: boolean;
  next_cursor: string | null;
}
export interface RecordHeader { id: string; created_at: string }
export interface Course extends RecordHeader { name: string }
export interface Assignment extends RecordHeader {
  course_id: string;
  classroom_id: string;
  classroom_name: string;
  title: string;
}
export type ItemStatus = "pending" | "running" | "retry_wait" | "succeeded" | "failed";
export type BatchStatus = "queued" | "running" | "settled";
export type ExportMode = "working" | "confirmed_only";
export interface Batch extends RecordHeader {
  rubric_version_id: string;
  question_version_id: string;
  assignment_id: string;
  assignment_title: string;
  course_id: string;
  status: BatchStatus;
  outcome: string | null;
  counts: Record<string, number>;
}
export interface BatchDetail {
  id: string;
  created_at: string;
  rubric_version_id: string;
  rubric_version: number;
  assignment: { id: string; title: string; course_id: string; classroom_id: string };
  question: { id: string; question_key: string; version: number };
  status: BatchStatus;
  outcome: string | null;
  counts: Record<string, number>;
  items: { id: string; status: ItemStatus; review_revision: number; error_code: string | null }[];
}
export interface ItemRow extends RecordHeader {
  answer_version_id: string;
  rubric_version_id: string;
  evaluation_profile: string;
  status: ItemStatus;
  error_code: string | null;
  suggested_score: string | null;
  confirmed_score: string | null;
  review_revision: number;
  review_status: "pending" | "confirmed";
  needs_review: boolean;
  simulation: boolean;
  student_id: string;
  student_name: string;
  external_student_id: string;
}
export interface Citation { quote: string; start: number; end: number }
export interface Level { id: string; ordinal: number; description: string; points: string }
export interface Criterion {
  id: string;
  semantic_version: number;
  description: string;
  maximum_points: string;
  levels: Level[];
}
export interface Rubric {
  global_instructions: string;
  criteria: Criterion[];
  selection_policy: string;
  rounding_policy: string;
}
export type EvidenceSupport = "supported" | "absence_confirmed" | "teacher_judgment";
export interface Decision {
  criterion_id: string;
  level_id: string;
  evidence_support: EvidenceSupport;
  reason: string;
  citations: Citation[];
}
export interface ReviewPayload { expected_revision: number; decisions: Decision[] }
export interface ReviewReceipt { id: string; revision: number; confirmed_score: string }
export interface Review {
  id: string;
  revision: number;
  decisions: Decision[];
  confirmed_score: string;
  created_at: string;
}
export interface Assessment {
  criterion_id: string;
  candidate_level_id: string | null;
  suggested_points: string | null;
  issues: string[];
  evidence: { citations: Citation[]; selection_status?: string; existence_verified: boolean };
  evaluation: {
    probabilities: Record<string, number> | null;
    provider_confidence: number | null;
    returned_model: string;
  };
}
export interface ItemDetail {
  id: string;
  status: ItemStatus;
  error_code: string | null;
  simulation: boolean;
  student: { id: string; name: string; external_id: string };
  assignment: { id: string; title: string; course_id: string; classroom_id: string };
  answer: { id: string; text: string; content_hash: string; source_revision: string };
  question: { id: string; question_key: string; version: number; prompt: string; reference: string };
  rubric: { id: string; version: number; body: Rubric };
  suggested_score: string | null;
  confirmed_score: string | null;
  review_revision: number;
  assessments: Assessment[];
  reviews: Review[];
  evaluation_profile: string;
  evaluation_profile_manifest: { model: string; simulation: boolean };
  execution: { attempts: number; calls_used: number; max_calls: number };
}
export interface ExportRow extends RecordHeader {
  batch_id: string;
  content_hash: string;
  mode: ExportMode;
  counts: Record<string, number>;
}
export interface ExportCreated {
  id: string;
  content_hash: string;
  snapshot: { snapshot_at: string; counts: Record<string, number>; mode: ExportMode };
}
export interface RetryPayload { reason: string; additional_attempts: number; additional_calls: number }

export interface RosterStudent { id: string; external_id: string; name: string }
export interface WorkspaceReceipt { course_id: string; classroom_id: string; students: RosterStudent[] }
export interface AssignmentDetail extends Assignment { course_name: string; roster: RosterStudent[] }
export interface QuestionRow extends RecordHeader {
  assignment_id: string; question_key: string; version: number; maximum_points: string;
  content_hash: string; prompt_preview: string;
}
export interface QuestionDetail extends Omit<QuestionRow, "prompt_preview"> { prompt: string; reference: string }
export interface VersionReceipt { id: string; version: number; content_hash: string }
export interface RubricRow extends RecordHeader {
  question_version_id: string; version: number; content_hash: string; criterion_count: number;
}
export interface RubricDetail extends Omit<RubricRow, "criterion_count"> { body: Rubric; confirmed_by: string }
export interface DraftRow extends RecordHeader {
  question_version_id: string; revision: number; confirmed_version_id: string | null;
  status: "draft" | "confirmed";
}
export interface DraftDetail extends Omit<DraftRow, "status"> { body: Rubric }
export interface DraftReceipt { id: string; revision: number; rubric: Rubric }
export interface ImportRow {
  student_id: string; question_version_id: string; external_submission_id: string;
  source_revision: string; answer_text: string;
}
export interface ImportPayload { rows: ImportRow[] }
export interface ImportPreview { valid: boolean; row_count: number; reused: number; notice: string }
export interface ImportReceipt { id: string; answer_version_ids: string[]; reused: number }
export interface AnswerRow extends RecordHeader {
  question_version_id: string; student_id: string; external_submission_id: string;
  source_revision: string; submitted_at: string | null; content_hash: string;
  answer_preview: string; student_name: string; external_student_id: string;
  question_key: string; question_version: number;
}
export interface AnswerDetail extends Omit<AnswerRow, "answer_preview" | "student_name" | "external_student_id" | "question_key" | "question_version"> {
  answer_text: string;
}
export interface EvaluationProfile {
  alias: string; id: string | null; ready: boolean; unavailable_reason?: string;
  manifest?: { provider: string; model: string; simulation: boolean; evidence_strategy: string };
}
export interface ProfileList { items: EvaluationProfile[]; default_alias: string }
export interface BatchReceipt { id: string; item_ids: string[]; evaluation_profile: string }

export type AgentStatus = "queued" | "running" | "waiting_teacher" | "completed" | "failed" | "cancelled";
export interface AgentProfile {
  alias: "fake-rubric-agent-v1" | "deepseek-rubric-agent-v1";
  id: string | null;
  ready: boolean;
  manifest?: { provider: string; model: string; revision?: string; call_timeout_seconds?: number };
}
export interface AgentProfileList { items: AgentProfile[]; default_alias: string }
export interface AgentLimits {
  max_model_calls: number; max_tool_calls: number;
  max_context_characters: number; max_seconds: number;
}
export interface AgentRun {
  id: string;
  question_version_id: string;
  status: AgentStatus;
  error_code: string | null;
  draft_id: string | null;
  source_draft?: { id: string; revision: number; content_hash: string } | null;
  revision_feedback?: string;
  waiting_for: string | null;
  profile_id: string;
  manifest: { provider: string; model: string; revision?: string };
  simulation: boolean;
  limits: AgentLimits;
  models_used: number;
  tools_used: number;
  attempts: number;
  epoch: number;
  event_sequence: number;
  expires_at: string;
  created_at: string;
}
export interface AgentEvent {
  sequence: number;
  epoch: number;
  kind: string;
  body: Record<string, unknown>;
  created_at: string;
}
export interface AgentEventPage { items: AgentEvent[]; next_after: number; has_more: boolean }
