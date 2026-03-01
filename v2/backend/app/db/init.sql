-- SQLite schema for the karaoke application.
-- Executed once at startup via app/db/__init__.py:init_db().
-- All tables use IF NOT EXISTS so reruns are safe.
-- No foreign-key constraints (denormalised per ADR-03).

-- === tracks ===
CREATE TABLE IF NOT EXISTS tracks (
    id TEXT PRIMARY KEY NOT NULL,
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    duration_sec INTEGER,
    mp3_path TEXT,
    instrumental_path TEXT,
    clip_path TEXT,
    lyrics_text TEXT,
    syllable_timings TEXT,  -- JSON array [{syllable, start, end}]
    language TEXT,
    source TEXT NOT NULL,  -- 'catalog' | 'user_upload'
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'processing' | 'ready' | 'error'
    error_message TEXT,
    play_count INTEGER NOT NULL DEFAULT 0,
    qdrant_synced INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks(status);
CREATE INDEX IF NOT EXISTS idx_tracks_source ON tracks(source);
CREATE INDEX IF NOT EXISTS idx_tracks_play_count ON tracks(play_count DESC) WHERE status = 'ready';
CREATE INDEX IF NOT EXISTS idx_tracks_artist_title ON tracks(artist, title);

-- FTS5 virtual table
CREATE VIRTUAL TABLE IF NOT EXISTS tracks_fts USING fts5(
    track_id UNINDEXED,
    artist,
    title,
    lyrics_text,
    content='tracks',
    content_rowid='rowid',
    tokenize='unicode61'
);

-- FTS sync triggers
CREATE TRIGGER IF NOT EXISTS tracks_ai AFTER INSERT ON tracks BEGIN
    INSERT INTO tracks_fts(rowid, track_id, artist, title, lyrics_text)
    VALUES (new.rowid, new.id, new.artist, new.title, new.lyrics_text);
END;

CREATE TRIGGER IF NOT EXISTS tracks_au AFTER UPDATE ON tracks BEGIN
    INSERT INTO tracks_fts(tracks_fts, rowid, track_id, artist, title, lyrics_text)
    VALUES ('delete', old.rowid, old.id, old.artist, old.title, old.lyrics_text);
    INSERT INTO tracks_fts(rowid, track_id, artist, title, lyrics_text)
    VALUES (new.rowid, new.id, new.artist, new.title, new.lyrics_text);
END;

-- === sessions ===
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY NOT NULL,
    room_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'terminated'
    created_at TEXT NOT NULL,
    terminated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_room_status ON sessions(room_id, status);

-- === participants ===
CREATE TABLE IF NOT EXISTS participants (
    id TEXT PRIMARY KEY NOT NULL,
    session_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    portrait_vector TEXT,  -- JSON float array (45-dim audio)
    lyrics_portrait_vector TEXT,  -- JSON float array (384-dim lyrics)
    tracks_played INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_participants_session ON participants(session_id);

-- === queue_entries ===
CREATE TABLE IF NOT EXISTS queue_entries (
    id TEXT PRIMARY KEY NOT NULL,
    session_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    track_id TEXT NOT NULL,
    order_position INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',  -- 'queued' | 'playing' | 'done' | 'skipped'
    added_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_queue_session_status ON queue_entries(session_id, status, order_position);

-- === play_history ===
CREATE TABLE IF NOT EXISTS play_history (
    id TEXT PRIMARY KEY NOT NULL,
    session_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    track_id TEXT NOT NULL,
    played_at TEXT NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_history_participant ON play_history(participant_id, played_at DESC);
CREATE INDEX IF NOT EXISTS idx_history_session ON play_history(session_id);
CREATE INDEX IF NOT EXISTS idx_history_track ON play_history(track_id);

-- === job_queue ===
CREATE TABLE IF NOT EXISTS job_queue (
    id TEXT PRIMARY KEY NOT NULL,
    track_id TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'running' | 'completed' | 'failed'
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    locked_by TEXT,
    locked_at TEXT,
    result TEXT,  -- JSON
    error_message TEXT,
    current_step TEXT,      -- e.g. 'separating', 'transcribing', 'generating_video'
    progress INTEGER NOT NULL DEFAULT 0,  -- 0-100
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_priority ON job_queue(status, priority DESC, created_at ASC)
    WHERE status = 'pending';
