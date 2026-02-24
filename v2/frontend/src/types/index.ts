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
  mp3_path?: string | null;
  instrumental_path?: string | null;
  clip_path?: string | null;
  lyrics_text?: string | null;
  language?: string | null;
  source: string; // "catalog" | "user_upload"
  status: string; // "pending" | "processing" | "ready" | "error"
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
}

export interface RecommendationResponse {
  strategy: 'popular' | 'last' | 'last_two_avg' | 'session_avg';
  tracks: RecommendedTrackItem[];
}

export interface SessionWithParticipants extends Session {
  participants: Participant[];
}
