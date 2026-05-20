export interface Session {
  id: string;
  room_id: string;
  status: string; // "active" | "terminated"
  created_at: string;
  terminated_at?: string | null;
}

export interface Participant {
  id: string;
  session_id: string;
  display_name: string;
  portrait_vector?: number[] | null;
  tracks_played: number;
  created_at: string;
}

export interface Track {
  id: string;
  artist: string;
  title: string;
  duration_sec: number | null;
  review_vocal_key?: string | null;
  mp3_path?: string | null;
  instrumental_path?: string | null;
  lyrics_text?: string | null;
  language?: string | null;
  source: string; // "catalog" | "user_upload"
  status: string; // "pending" | "processing" | "ready" | "error"
  alignment_review_status?: string;
  review_requested_at?: string | null;
  review_completed_at?: string | null;
  play_count: number;
  created_at: string;
  updated_at: string;
}

export type QueueEntryStatus = 'queued' | 'playing' | 'done' | 'skipped';

export interface QueueEntry {
  id: string;
  session_id: string;
  participant_id: string;
  track_id: string;
  order_position: number;
  status: QueueEntryStatus;
  added_at: string;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface QueueEntryWithDetails {
  id: string;
  session_id: string;
  order_position: number;
  status: string;
  added_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  participant: Participant | null;
  track: Track | null;
}

export interface QueueResponse {
  current: QueueEntryWithDetails | null;
  upcoming: QueueEntryWithDetails[];
}

export interface RecommendedTrackItem {
  id: string;
  artist: string;
  title: string;
  duration_sec: number | null;
  similarity_score: number;
  artist_image_url: string | null;
}

export interface RecommendationResponse {
  strategy: 'popular' | 'cluster';
  tracks: RecommendedTrackItem[];
}

export interface MoodTag {
  id: number;
  name: string;
}

export interface SessionWithParticipants extends Session {
  participants: Participant[];
}

export interface TrackSearchItem {
  id: string;
  artist: string;
  title: string;
  duration_sec: number | null;
  language: string | null;
  source: string;
  clip_ready: boolean;
  artist_image_url: string | null;
}

export interface SearchResult {
  total: number;
  items: TrackSearchItem[];
}

export interface UploadResponse {
  track_id: string;
  job_id: string;
  status: string;
}

export interface ActiveJob {
  job_id: string;
  track_id: string;
  status: string;
  current_step: string | null;
  progress: number;
  artist: string;
  title: string;
}

export interface JobStatusEvent {
  job_id: string;
  status: string;
  step?: string;
  progress?: number;
  track_id?: string;
  clip_url?: string | null;
  error?: string;
  result?: unknown;
}

export interface SyllableTiming {
  syllable: string;
  start: number;
  end: number;
}

export interface StartPlayingResponse {
  entry_id: string;
  clip_url: string | null;
  syllable_timings: SyllableTiming[] | null;
  duration_sec: number | null;
  title: string | null;
  artist: string | null;
  lyrics_source: string | null;
}

export interface FinishPlayingResponse {
  next_participant: Participant | null;
  next_entry_id: string | null;
}

export interface HistoryItem {
  track_id: string;
  artist: string;
  title: string;
  duration_sec: number | null;
  artist_image_url: string | null;
  played_at: string;
  source: string;
}

export interface AlignmentSyllable {
  id: string;
  text: string;
  start: number;
  end: number;
  word_id: string;
  line_id: string;
  flags: string[];
}

export interface AlignmentWord {
  id: string;
  text: string;
  start: number;
  end: number;
  line_id: string;
  syllable_ids: string[];
  flags: string[];
}

export interface AlignmentLine {
  id: string;
  text: string;
  start: number;
  end: number;
  word_ids: string[];
  flags: string[];
}

export interface AlignmentSection {
  id: string;
  title?: string | null;
  line_ids: string[];
}

export interface AlignmentDocument {
  sections: AlignmentSection[];
  lines: AlignmentLine[];
  words: AlignmentWord[];
  syllables: AlignmentSyllable[];
}

export interface AlignmentRevision {
  id: string;
  track_id: string;
  revision_no: number;
  source: string;
  lyrics_text?: string | null;
  syllable_timings: SyllableTiming[];
  document?: AlignmentDocument | null;
  operations: Record<string, unknown>[];
  diagnostics: Record<string, unknown>;
  is_published: boolean;
  created_by?: string | null;
  created_at: string;
  updated_at: string;
  published_at?: string | null;
}

export interface AlignmentTrackSummary {
  id: string;
  artist: string;
  title: string;
  duration_sec: number | null;
  lyrics_source: string | null;
  review_vocal_key?: string | null;
  source: string;
  status: string;
  alignment_review_status: string;
  review_requested_at?: string | null;
  review_completed_at?: string | null;
}

export interface AlignmentReviewQueueItem {
  id: string;
  artist: string;
  title: string;
  duration_sec: number | null;
  lyrics_source: string | null;
  alignment_review_status: string;
  review_requested_at?: string | null;
  source: string;
}

export interface AlignmentEditorPayload {
  track: AlignmentTrackSummary;
  stream_url: string | null;
  stream_source: 'vocals' | 'instrumental' | string;
  lyrics_text: string | null;
  syllable_timings: SyllableTiming[];
  document: AlignmentDocument;
  active_revision: AlignmentRevision | null;
  revisions: AlignmentRevision[];
}

export interface SaveAlignmentDraftRequest {
  document: AlignmentDocument;
  operations: Record<string, unknown>[];
  diagnostics: Record<string, unknown>;
  created_by?: string | null;
}

export interface RealignLyricsResponse {
  job_id: string;
}

export interface RealignSyllablesFragmentJobResponse {
  job_id: string;
}

export interface RealignSyllablesFragmentRequest {
  audio_start: number;
  audio_end: number;
  line_ids: string[];
  text: string;
  preserve_line_breaks: boolean;
}

export interface RealignSyllablesFragmentResponse {
  timing_origin: 'relative_to_fragment' | 'absolute_track_time';
  audio_start: number;
  audio_end: number;
  status: 'ok' | 'partial' | 'failed';
  confidence?: number | null;
  syllable_timings: SyllableTiming[];
  line_mapping?: {
    line_id: string;
    syllable_start_index: number;
    syllable_end_index: number;
  }[] | null;
  warnings: string[];
}

export type AutoRepairMode = 'analyze_only' | 'propose' | 'auto_apply_safe';
export type AutoRepairDecision = 'auto_apply' | 'needs_review' | 'rejected' | 'blocked';

export interface AutoRepairAlignmentRequest {
  revision_id?: string | null;
  mode: AutoRepairMode;
  max_cluster_lines?: number;
  max_audio_seconds?: number;
  max_ctc_candidates?: number;
  auto_apply_threshold?: number;
  review_threshold?: number;
}

export interface AutoRepairJobResponse {
  job_id: string;
}

export interface AutoRepairRange {
  start: number;
  end: number;
}

export interface AutoRepairLineMapping {
  line_id: string;
  syllable_start_index: number;
  syllable_end_index: number;
}

export interface AlignmentDocumentPatch {
  replace_lines: AlignmentLine[];
  replace_words: AlignmentWord[];
  replace_syllables: AlignmentSyllable[];
  remove_word_ids: string[];
  remove_syllable_ids: string[];
}

export interface AutoRepairCluster {
  id: string;
  line_ids: string[];
  start_line_index: number;
  end_line_index: number;
  old_audio_range: AutoRepairRange;
  flags: string[];
  root_cause_hints: string[];
}

export interface AutoRepairProposal {
  id: string;
  cluster_id: string;
  decision: AutoRepairDecision;
  root_cause_hints: string[];
  score: number;
  confidence: number;
  margin: number;
  line_ids: string[];
  text: string;
  old_audio_range: AutoRepairRange;
  new_audio_range: AutoRepairRange;
  timing_origin: 'relative_to_fragment';
  syllable_timings: SyllableTiming[];
  line_mapping?: AutoRepairLineMapping[] | null;
  document_patch: AlignmentDocumentPatch;
  locator_method?: string | null;
  matched_text?: string | null;
  locator_confidence?: number | null;
  phoneme_score?: number | null;
  text_score?: number | null;
  reasons: string[];
  warnings: string[];
}

export interface AutoRepairSummary {
  clusters: number;
  auto_apply: number;
  needs_review: number;
  rejected: number;
  blocked: number;
}

export interface AutoRepairReport {
  job_id: string;
  track_id: string;
  base_revision_id: string;
  source_audio_key: string;
  status: 'ok' | 'partial' | 'failed';
  created_revision_id?: string | null;
  summary: AutoRepairSummary;
  clusters: AutoRepairCluster[];
  proposals: AutoRepairProposal[];
  warnings: string[];
}

export interface ApplyAutoRepairRequest {
  job_id: string;
  base_revision_id: string;
  proposal_ids: string[];
  created_by?: string | null;
}
