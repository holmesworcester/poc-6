-- Packet metadata staging table
--
-- Stores source address information during event processing.
-- Connection projection copies this to connections table.

CREATE TABLE IF NOT EXISTS packet_metadata (
    event_id TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    from_addr_ip TEXT,
    from_addr_port INTEGER,
    PRIMARY KEY (event_id, recorded_by)
);

-- Index for cleanup
CREATE INDEX IF NOT EXISTS idx_packet_metadata_recorded_by
ON packet_metadata(recorded_by);
