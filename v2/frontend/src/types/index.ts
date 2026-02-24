export interface Session {
  id: string;
  room_id: string;
  created_at: string;
  is_active: boolean;
}

export interface Participant {
  id: string;
  session_id: string;
  display_name: string;
  created_at: string;
}

export interface Track {
  id: string;
  title: string;
  artist: string;
  duration_seconds: number | null;
  file_path: string | null;
  vocals_path: string | null;
  instrumental_path: string | null;
  lyrics_path: string | null;
  source_url: string | null;
  created_at: string;
}

export type QueueEntryStatus = 'waiting' | 'playing' | 'done' | 'skipped';

export interface QueueEntry {
  id: string;
  session_id: string;
  participant_id: string;
  track_id: string;
  status: QueueEntryStatus;
  position: number;
  created_at: string;
}

export interface QueueEntryWithDetails extends QueueEntry {
  participant: Participant;
  track: Track;
}

export interface QueueResponse {
  current: QueueEntryWithDetails | null;
  upcoming: QueueEntryWithDetails[];
}

export interface RecommendedTrack {
  track: Track;
  score: number;
  reason: string;
}

export interface RecommendationResponse {
  participant_id: string;
  recommendations: RecommendedTrack[];
}

export interface SessionWithParticipants extends Session {
  participants: Participant[];
}
