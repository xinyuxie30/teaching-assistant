export interface BankQuestion {
  id: string; course_id: string; title: string; prompt: string; expected_answer: string;
  knowledge_points: string[]; grade: string; difficulty: string; question_type: string;
  source: string; content_hash: string; confirmed_at: string | null; retired_at: string | null;
}
export interface Memory {
  id: string; title: string; body: string; content_hash: string; confirmed_at: string | null;
  source: { note: string }; valid_until: string | null;
}
export interface Evidence {
  id: string; batch_id: string; description: string; confirmed_students: number;
  below_full_credit_students: number; below_full_credit_rate: string;
}
export interface Basis {
  batch_id: string; basis_hash: string; scope: { assignment_title: string; question_key: string };
  coverage: { confirmed: number; batch_students: number; simulated_confirmed: number };
}
export interface Candidate {
  question: BankQuestion; score: number;
  evidence_matches: { evidence_id: string; terms: string[] }[];
  topic_matches: string[]; memory_ids: string[];
}
export interface Preview {
  preview_hash: string; candidates: Candidate[]; matching_count: number;
  evidence: Evidence[]; memories: Memory[]; batches: Basis[];
}
export interface PracticeBody {
  title: string; teaching_notes: string; simulation: boolean; classroom_name: string;
  evidence: Evidence[]; batches: Basis[]; memories: Memory[];
  questions: (Candidate & { reason: string })[];
}
export interface PracticeArtifact {
  id: string; body: PracticeBody; content_hash: string; created_at: string; confirmed_at: string | null;
}
