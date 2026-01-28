-- Raw ingest log for fast-path receive (no unwrap/parse)
CREATE TABLE IF NOT EXISTS incoming_event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hint BLOB NOT NULL,            -- Transit key hint
    recorded_by TEXT NOT NULL,
    received_at INTEGER NOT NULL,
    source_ip TEXT,
    source_port INTEGER,
    event_type TEXT,               -- Optional protocol marker (e.g. negentropy)
    blob BLOB NOT NULL,
    transit_wrapped INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_incoming_event_log_hint
ON incoming_event_log(hint);

CREATE INDEX IF NOT EXISTS idx_incoming_event_log_received
ON incoming_event_log(received_at);

-- Materialized mapping from ingest log to stored events
CREATE TABLE IF NOT EXISTS ingest_index (
    log_id INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL,
    recorded_id TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    hint BLOB NOT NULL,            -- Event-layer key hint (or empty for plaintext)
    event_type TEXT,               -- Plaintext type when available
    received_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ingest_index_hint
ON ingest_index(hint);

CREATE INDEX IF NOT EXISTS idx_ingest_index_event_type
ON ingest_index(event_type);

CREATE INDEX IF NOT EXISTS idx_ingest_index_recorded
ON ingest_index(recorded_by);

-- Per-stream cursors for priority and GC projection
CREATE TABLE IF NOT EXISTS projection_streams (
    stream_name TEXT PRIMARY KEY,
    last_log_id INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);

-- UI active channel tracking + update notifications
CREATE TABLE IF NOT EXISTS ui_active_channel (
    peer_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ui_channel_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ui_channel_updates_peer
ON ui_channel_updates(peer_id, channel_id, created_at);
