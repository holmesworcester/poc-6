-- Schema for framework_tests protocol
-- This defines the SQL tables that correspond to the dict-based storage used in handlers

-- Identity management
CREATE TABLE IF NOT EXISTS identities (
    pubkey VARCHAR(64) PRIMARY KEY,
    privkey VARCHAR(64) NOT NULL,
    name VARCHAR(255) NOT NULL,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);

-- Peer relationships
CREATE TABLE IF NOT EXISTS peers (
    pubkey VARCHAR(64) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    joined_via VARCHAR(50) DEFAULT 'direct',
    added_at BIGINT NOT NULL,
    FOREIGN KEY (pubkey) REFERENCES identities(pubkey) ON DELETE CASCADE
);

-- Messages between peers
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id VARCHAR(64) UNIQUE NOT NULL,
    text TEXT NOT NULL,
    sender VARCHAR(64) NOT NULL,
    recipient VARCHAR(64),
    reply_to VARCHAR(64),
    timestamp BIGINT NOT NULL,
    sig VARCHAR(128) NOT NULL,
    unknown_peer BOOLEAN DEFAULT FALSE,
    created_at BIGINT NOT NULL,
    INDEX idx_messages_sender (sender),
    INDEX idx_messages_recipient (recipient),
    INDEX idx_messages_timestamp (timestamp),
    INDEX idx_messages_sender_timestamp (sender, timestamp),
    FOREIGN KEY (sender) REFERENCES identities(pubkey) ON DELETE CASCADE,
    FOREIGN KEY (recipient) REFERENCES identities(pubkey) ON DELETE CASCADE
);

-- Known senders whitelist (stored as array in handlers, but we track as table)
-- Note: In dict mode this is just an array of pubkey strings
CREATE TABLE IF NOT EXISTS known_senders (
    pubkey VARCHAR(64) PRIMARY KEY,
    added_at BIGINT NOT NULL,
    FOREIGN KEY (pubkey) REFERENCES identities(pubkey) ON DELETE CASCADE
);

-- Encryption key mapping
CREATE TABLE IF NOT EXISTS key_map (
    key_hash VARCHAR(64) PRIMARY KEY,
    key_value VARCHAR(64) NOT NULL,
    created_at BIGINT NOT NULL
);

-- Events pending decryption due to missing keys
-- Note: Handler uses camelCase field names (missingHash, inNetwork)
CREATE TABLE IF NOT EXISTS pending_missing_key (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    envelope TEXT NOT NULL,
    missingHash VARCHAR(64) NOT NULL,  -- Using handler field name
    inNetwork BOOLEAN NOT NULL,         -- Using handler field name
    timestamp BIGINT NOT NULL,
    origin VARCHAR(255),
    INDEX idx_pending_hash (missingHash),
    INDEX idx_pending_timestamp (timestamp)
);

-- Unknown/unrecognized events
CREATE TABLE IF NOT EXISTS unknown_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data TEXT NOT NULL,
    metadata TEXT NOT NULL,
    timestamp BIGINT NOT NULL,
    INDEX idx_unknown_timestamp (timestamp)
);

-- Event store for event sourcing
CREATE TABLE IF NOT EXISTS event_store (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pubkey VARCHAR(64) NOT NULL,
    event_data TEXT NOT NULL,
    event_type VARCHAR(50) NOT NULL,
    event_id VARCHAR(64) UNIQUE NOT NULL,
    created_at BIGINT NOT NULL,
    INDEX idx_event_store_pubkey (pubkey),
    INDEX idx_event_store_type (event_type),
    INDEX idx_event_store_created (created_at),
    INDEX idx_event_store_pubkey_created (pubkey, created_at),
    FOREIGN KEY (pubkey) REFERENCES identities(pubkey) ON DELETE CASCADE
);

-- Incoming message queue
CREATE TABLE IF NOT EXISTS incoming (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data TEXT NOT NULL,
    origin VARCHAR(255),
    received_at BIGINT NOT NULL,
    envelope BOOLEAN DEFAULT FALSE,
    processed BOOLEAN DEFAULT FALSE,
    INDEX idx_incoming_received (received_at),
    INDEX idx_incoming_processed (processed)
);

-- Outgoing message queue
CREATE TABLE IF NOT EXISTS outgoing (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient VARCHAR(64) NOT NULL,
    data TEXT NOT NULL,
    created_at BIGINT NOT NULL,
    sent BOOLEAN DEFAULT FALSE,
    INDEX idx_outgoing_recipient (recipient),
    INDEX idx_outgoing_created (created_at),
    INDEX idx_outgoing_sent (sent)
);