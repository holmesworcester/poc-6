CREATE TABLE IF NOT EXISTS message_attachments (
    message_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    filename TEXT,
    mime_type TEXT,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (message_id, file_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_message_attachments_message
ON message_attachments(message_id, recorded_by);

CREATE INDEX IF NOT EXISTS idx_message_attachments_file
ON message_attachments(file_id, recorded_by);
