-- PostgreSQL schema for the karaoke application.
-- Executed once at startup via app/db/__init__.py:init_pg().
-- All tables use IF NOT EXISTS so reruns are safe.
-- No foreign-key constraints (denormalised per ADR-03).

-- === tracks ===
CREATE TABLE IF NOT EXISTS tracks (
    id TEXT PRIMARY KEY NOT NULL,
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    duration_sec INTEGER,
    instrumental_key TEXT,
    review_vocal_key TEXT,
    lyrics_text TEXT,
    syllable_timings JSONB,
    language TEXT,
    source TEXT NOT NULL,           -- 'catalog' | 'user_upload'
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'processing' | 'ready' | 'error'
    error_message TEXT,
    play_count INTEGER NOT NULL DEFAULT 0,
    qdrant_synced INTEGER NOT NULL DEFAULT 0,
    popularity_category TEXT NOT NULL DEFAULT 'regular',
    chart_count INTEGER NOT NULL DEFAULT 0,
    chart_last_seen TIMESTAMPTZ,
    catalog_cluster_id INTEGER,
    rec_cluster_id INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks(status);
CREATE INDEX IF NOT EXISTS idx_tracks_source ON tracks(source);
CREATE INDEX IF NOT EXISTS idx_tracks_play_count ON tracks(play_count DESC) WHERE status = 'ready';
CREATE INDEX IF NOT EXISTS idx_tracks_artist_title ON tracks(artist, title);
CREATE INDEX IF NOT EXISTS idx_tracks_cluster ON tracks(catalog_cluster_id);
CREATE INDEX IF NOT EXISTS idx_tracks_rec_cluster ON tracks(rec_cluster_id);
CREATE INDEX IF NOT EXISTS idx_tracks_popularity ON tracks(popularity_category) WHERE status = 'ready';

-- Lyrics provenance: where the final lyrics text came from.
-- Values: provider name ("lrclib", "genius", "lyricsovh", "agent", ...) for
-- candidates that passed the algorithmic matcher; "asr_fallback" when no
-- candidate qualified and we used the raw Whisper transcription instead
-- (text may contain ASR errors, candidates for re-processing).
ALTER TABLE tracks ADD COLUMN IF NOT EXISTS lyrics_source text;
CREATE INDEX IF NOT EXISTS idx_tracks_lyrics_source ON tracks(lyrics_source);

-- Lead/full vocal stem used by the alignment editor and automated repair jobs.
ALTER TABLE tracks ADD COLUMN IF NOT EXISTS review_vocal_key text;

-- Full-text search via tsvector
ALTER TABLE tracks ADD COLUMN IF NOT EXISTS search_vector tsvector;

CREATE INDEX IF NOT EXISTS idx_tracks_fts ON tracks USING GIN(search_vector);

-- Trigger to auto-update search_vector on INSERT or UPDATE
CREATE OR REPLACE FUNCTION tracks_search_vector_update() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('simple', COALESCE(NEW.artist, '')), 'A') ||
        setweight(to_tsvector('simple', COALESCE(NEW.title, '')), 'B') ||
        setweight(to_tsvector('simple', COALESCE(NEW.lyrics_text, '')), 'C');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tracks_search_vector_trigger ON tracks;
CREATE TRIGGER tracks_search_vector_trigger
    BEFORE INSERT OR UPDATE OF artist, title, lyrics_text ON tracks
    FOR EACH ROW EXECUTE FUNCTION tracks_search_vector_update();

-- Backfill search_vector for existing rows (safe to re-run)
UPDATE tracks SET search_vector =
    setweight(to_tsvector('simple', COALESCE(artist, '')), 'A') ||
    setweight(to_tsvector('simple', COALESCE(title, '')), 'B') ||
    setweight(to_tsvector('simple', COALESCE(lyrics_text, '')), 'C')
WHERE search_vector IS NULL;


-- === sessions ===
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY NOT NULL,
    room_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'terminated'
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    terminated_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_sessions_room_status ON sessions(room_id, status);


-- === participants ===
CREATE TABLE IF NOT EXISTS participants (
    id TEXT PRIMARY KEY NOT NULL,
    session_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    portrait_vector JSONB,          -- float array (45-dim audio)
    lyrics_portrait_vector JSONB,   -- float array (384-dim lyrics)
    tracks_played INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_queue_session_status ON queue_entries(session_id, status, order_position);


-- === play_history ===
CREATE TABLE IF NOT EXISTS play_history (
    id TEXT PRIMARY KEY NOT NULL,
    session_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    track_id TEXT NOT NULL,
    played_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_history_participant ON play_history(participant_id, played_at DESC);
CREATE INDEX IF NOT EXISTS idx_history_session ON play_history(session_id);
CREATE INDEX IF NOT EXISTS idx_history_track ON play_history(track_id);


-- === job_queue ===
CREATE TABLE IF NOT EXISTS job_queue (
    id TEXT PRIMARY KEY NOT NULL,
    track_id TEXT,                   -- NULL until worker finalisation (deferred track creation)
    mp3_key TEXT,                    -- S3 object key for the uploaded MP3
    artist_hint TEXT,                -- user-provided artist (from upload form)
    title_hint TEXT,                 -- user-provided title (from upload form)
    priority INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'running' | 'completed' | 'failed'
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    locked_by TEXT,
    locked_at TIMESTAMPTZ,
    data JSONB,                      -- intermediate pipeline data (instrumental_key, lyrics, etc.)
    result JSONB,                    -- final result payload
    error_message TEXT,
    current_step TEXT,               -- e.g. 'separating', 'transcribing'
    progress INTEGER NOT NULL DEFAULT 0,  -- 0-100
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_priority ON job_queue(status, priority DESC, created_at ASC)
    WHERE status = 'pending';


-- === catalog_clusters ===
CREATE TABLE IF NOT EXISTS catalog_clusters (
    id SERIAL PRIMARY KEY,
    centroid_audio JSONB NOT NULL,
    centroid_lyrics JSONB NOT NULL,
    track_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- === artists ===
CREATE TABLE IF NOT EXISTS artists (
    name TEXT PRIMARY KEY NOT NULL,
    image_path TEXT,
    source TEXT,                     -- 'spotify' | 'yandex' | 'placeholder'
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- === mood_tags ===
CREATE TABLE IF NOT EXISTS mood_tags (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    cluster_id INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(name, cluster_id)
);

CREATE INDEX IF NOT EXISTS idx_mood_tags_cluster ON mood_tags(cluster_id);


-- === api_costs ===
CREATE TABLE IF NOT EXISTS api_costs (
    id SERIAL PRIMARY KEY,
    track_id TEXT NOT NULL,
    service TEXT NOT NULL,
    cost_usd REAL NOT NULL DEFAULT 0,
    tokens INTEGER,
    duration_sec REAL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_api_costs_service ON api_costs(service, created_at);

-- === alignment revisions ===
CREATE TABLE IF NOT EXISTS alignment_revisions (
    id TEXT PRIMARY KEY NOT NULL,
    track_id TEXT NOT NULL,
    revision_no INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    lyrics_text TEXT,
    syllable_timings JSONB NOT NULL DEFAULT '[]'::jsonb,
    document JSONB,
    operations JSONB NOT NULL DEFAULT '[]'::jsonb,
    diagnostics JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_published BOOLEAN NOT NULL DEFAULT FALSE,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_alignment_revisions_track_id
    ON alignment_revisions(track_id, revision_no DESC);
CREATE INDEX IF NOT EXISTS idx_alignment_revisions_track_published
    ON alignment_revisions(track_id) WHERE is_published = TRUE;
CREATE UNIQUE INDEX IF NOT EXISTS idx_alignment_revisions_track_revision_no
    ON alignment_revisions(track_id, revision_no);
