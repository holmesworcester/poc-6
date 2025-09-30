-- Schema for message_via_tor protocol
-- This defines the SQL tables that correspond to the dict-based storage used in handlers

-- Identity management (includes keypairs for tor protocol)
CREATE TABLE IF NOT EXISTS identities (
    pubkey VARCHAR(64) PRIMARY KEY,
    privkey VARCHAR(64) NOT NULL,
    name VARCHAR(255) NOT NULL,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);

-- Messages with tor-specific recipient field
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id VARCHAR(64) UNIQUE NOT NULL,
    text TEXT NOT NULL,
    sender VARCHAR(64) NOT NULL,
    recipient VARCHAR(64),  -- Optional, routing handled by envelopes
    received_by VARCHAR(64) NOT NULL,  -- Which identity received this message
    timestamp BIGINT NOT NULL,
    sig VARCHAR(128) NOT NULL,
    unknown_peer BOOLEAN DEFAULT FALSE,
    created_at BIGINT NOT NULL,
    INDEX idx_messages_sender (sender),
    INDEX idx_messages_recipient (recipient),
    INDEX idx_messages_received_by (received_by),
    INDEX idx_messages_timestamp (timestamp),
    INDEX idx_messages_sender_recipient (sender, recipient)
    -- Note: No foreign key constraints on sender/recipient as messages can come from unknown peers
);

-- Outgoing message queue for tor routing
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

-- Event store for event sourcing
CREATE TABLE IF NOT EXISTS event_store (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pubkey VARCHAR(64) NOT NULL,
    event_data TEXT NOT NULL,
    metadata TEXT NOT NULL,
    event_type VARCHAR(50) NOT NULL,
    event_id VARCHAR(64) UNIQUE NOT NULL,
    created_at BIGINT NOT NULL,
    INDEX idx_event_store_pubkey (pubkey),
    INDEX idx_event_store_type (event_type),
    INDEX idx_event_store_created (created_at),
    INDEX idx_event_store_pubkey_created (pubkey, created_at)
);

-- Peer relationships (per identity - tracks which identity knows which peers)
CREATE TABLE IF NOT EXISTS peers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pubkey VARCHAR(64) NOT NULL,
    name VARCHAR(255) NOT NULL,
    joined_via VARCHAR(50) DEFAULT 'direct',
    added_at BIGINT NOT NULL,
    received_by VARCHAR(64) NOT NULL,  -- Which identity received this peer event
    INDEX idx_peers_pubkey (pubkey),
    INDEX idx_peers_received_by (received_by),
    INDEX idx_peers_pubkey_received_by (pubkey, received_by),
    UNIQUE (pubkey, received_by)  -- Each identity can only know a peer once
);

-- Unknown/unrecognized events
CREATE TABLE IF NOT EXISTS unknown_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data TEXT NOT NULL,
    metadata TEXT NOT NULL,
    timestamp BIGINT NOT NULL,
    INDEX idx_unknown_timestamp (timestamp)
);

-- Incoming queue for network-delivered envelopes
CREATE TABLE IF NOT EXISTS incoming (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient VARCHAR(64) NOT NULL,
    data TEXT NOT NULL,
    metadata TEXT NOT NULL,
    received_at BIGINT NOT NULL,
    INDEX idx_incoming_recipient (recipient),
    INDEX idx_incoming_received_at (received_at)
);
