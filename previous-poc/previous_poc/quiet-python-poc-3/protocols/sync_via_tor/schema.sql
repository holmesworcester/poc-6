-- Schema for sync_via_tor protocol
-- This defines the SQL tables that correspond to the dict-based storage used in handlers
-- Extends message_via_tor with additional sync and advanced features

-- Identity management (includes keypairs for tor protocol)
CREATE TABLE IF NOT EXISTS identities (
    pubkey VARCHAR(64) PRIMARY KEY,
    privkey VARCHAR(64) NOT NULL,
    name VARCHAR(255) NOT NULL,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);

-- Peer relationships with sync metadata
CREATE TABLE IF NOT EXISTS peers (
    pubkey VARCHAR(64) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    joined_via VARCHAR(50) DEFAULT 'direct',
    added_at BIGINT NOT NULL,
    last_sync BIGINT,
    sync_cursor VARCHAR(64),  -- For lazy sync pagination
    FOREIGN KEY (pubkey) REFERENCES identities(pubkey) ON DELETE CASCADE
);

-- Messages with tor-specific fields and TTL support
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id VARCHAR(64) UNIQUE NOT NULL,
    text TEXT NOT NULL,
    sender VARCHAR(64) NOT NULL,
    recipient VARCHAR(64),  -- Optional, routing handled by envelopes
    reply_to VARCHAR(64),
    timestamp BIGINT NOT NULL,
    sig VARCHAR(128) NOT NULL,
    unknown_peer BOOLEAN DEFAULT FALSE,
    ttl BIGINT,  -- Time-to-live for disappearing messages
    created_at BIGINT NOT NULL,
    INDEX idx_messages_sender (sender),
    INDEX idx_messages_recipient (recipient),
    INDEX idx_messages_timestamp (timestamp),
    INDEX idx_messages_sender_recipient (sender, recipient),
    INDEX idx_messages_ttl (ttl),
    FOREIGN KEY (sender) REFERENCES identities(pubkey) ON DELETE CASCADE,
    FOREIGN KEY (recipient) REFERENCES identities(pubkey) ON DELETE CASCADE
);

-- Known senders whitelist
CREATE TABLE IF NOT EXISTS known_senders (
    pubkey VARCHAR(64) PRIMARY KEY,
    added_at BIGINT NOT NULL,
    FOREIGN KEY (pubkey) REFERENCES identities(pubkey) ON DELETE CASCADE
);

-- Encryption key mapping with support for group keys
CREATE TABLE IF NOT EXISTS key_map (
    key_hash VARCHAR(64) PRIMARY KEY,
    key_value VARCHAR(64) NOT NULL,
    key_type VARCHAR(20) DEFAULT 'standard',  -- standard, group, sealed
    created_at BIGINT NOT NULL
);

-- Private group keys (sealed to specific recipients)
CREATE TABLE IF NOT EXISTS sealed_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash VARCHAR(64) NOT NULL,
    recipient_pubkey VARCHAR(64) NOT NULL,
    sealed_data TEXT NOT NULL,
    created_at BIGINT NOT NULL,
    INDEX idx_sealed_keys_hash (key_hash),
    INDEX idx_sealed_keys_recipient (recipient_pubkey),
    FOREIGN KEY (recipient_pubkey) REFERENCES identities(pubkey) ON DELETE CASCADE
);

-- Blob storage for attachments
CREATE TABLE IF NOT EXISTS blobs (
    blob_id VARCHAR(64) PRIMARY KEY,
    message_id VARCHAR(64) NOT NULL,
    total_slices INTEGER NOT NULL,
    received_slices INTEGER DEFAULT 0,
    content_type VARCHAR(100),
    created_at BIGINT NOT NULL,
    completed_at BIGINT,
    INDEX idx_blobs_message (message_id),
    FOREIGN KEY (message_id) REFERENCES messages(event_id) ON DELETE CASCADE
);

-- Blob slices for chunked transfer
CREATE TABLE IF NOT EXISTS blob_slices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    blob_id VARCHAR(64) NOT NULL,
    slice_index INTEGER NOT NULL,
    slice_data TEXT NOT NULL,
    received_at BIGINT NOT NULL,
    UNIQUE(blob_id, slice_index),
    INDEX idx_blob_slices_blob (blob_id),
    FOREIGN KEY (blob_id) REFERENCES blobs(blob_id) ON DELETE CASCADE
);

-- Sync state tracking
CREATE TABLE IF NOT EXISTS sync_state (
    peer_pubkey VARCHAR(64) PRIMARY KEY,
    last_event_id VARCHAR(64),
    last_sync_request BIGINT,
    last_sync_response BIGINT,
    bloom_filter TEXT,  -- Serialized bloom filter for efficient sync
    bloom_salt VARCHAR(32),  -- Random salt for bloom filter
    INDEX idx_sync_state_last_request (last_sync_request),
    FOREIGN KEY (peer_pubkey) REFERENCES peers(pubkey) ON DELETE CASCADE
);

-- Events pending decryption due to missing keys
CREATE TABLE IF NOT EXISTS pending_missing_key (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    envelope TEXT NOT NULL,
    missingHash VARCHAR(64) NOT NULL,
    inNetwork BOOLEAN NOT NULL,
    timestamp BIGINT NOT NULL,
    origin VARCHAR(255),
    INDEX idx_pending_hash (missingHash),
    INDEX idx_pending_timestamp (timestamp)
);

-- Removed peers tracking
CREATE TABLE IF NOT EXISTS removed_peers (
    pubkey VARCHAR(64) PRIMARY KEY,
    removed_by VARCHAR(64) NOT NULL,
    removed_at BIGINT NOT NULL,
    reason TEXT,
    INDEX idx_removed_peers_removed_by (removed_by),
    INDEX idx_removed_peers_removed_at (removed_at)
);

-- Multi-device linking
CREATE TABLE IF NOT EXISTS device_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_pubkey VARCHAR(64) NOT NULL,
    device_pubkey VARCHAR(64) NOT NULL,
    device_name VARCHAR(255),
    linked_at BIGINT NOT NULL,
    linked_by VARCHAR(64) NOT NULL,
    UNIQUE(user_pubkey, device_pubkey),
    INDEX idx_device_links_user (user_pubkey),
    INDEX idx_device_links_device (device_pubkey),
    FOREIGN KEY (user_pubkey) REFERENCES identities(pubkey) ON DELETE CASCADE,
    FOREIGN KEY (device_pubkey) REFERENCES identities(pubkey) ON DELETE CASCADE
);

-- Event store for event sourcing with signature tracking
CREATE TABLE IF NOT EXISTS event_store (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pubkey VARCHAR(64) NOT NULL,
    event_data TEXT NOT NULL,
    event_type VARCHAR(50) NOT NULL,
    event_id VARCHAR(64) UNIQUE NOT NULL,
    event_sig VARCHAR(128),  -- For signed events
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

-- Outgoing message queue for tor routing
CREATE TABLE IF NOT EXISTS outgoing (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient VARCHAR(64) NOT NULL,
    data TEXT NOT NULL,
    created_at BIGINT NOT NULL,
    sent BOOLEAN DEFAULT FALSE,
    retry_count INTEGER DEFAULT 0,
    next_retry BIGINT,
    INDEX idx_outgoing_recipient (recipient),
    INDEX idx_outgoing_created (created_at),
    INDEX idx_outgoing_sent (sent),
    INDEX idx_outgoing_next_retry (next_retry)
);

-- Unknown/unrecognized events
CREATE TABLE IF NOT EXISTS unknown_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data TEXT NOT NULL,
    metadata TEXT NOT NULL,
    timestamp BIGINT NOT NULL,
    INDEX idx_unknown_timestamp (timestamp)
);

-- Invite tracking for proof-of-invitation
CREATE TABLE IF NOT EXISTS invites (
    invite_code VARCHAR(64) PRIMARY KEY,
    inviter_pubkey VARCHAR(64) NOT NULL,
    invitee_pubkey VARCHAR(64),
    invite_type VARCHAR(20) DEFAULT 'peer',  -- peer, device_link
    created_at BIGINT NOT NULL,
    used_at BIGINT,
    expires_at BIGINT,
    INDEX idx_invites_inviter (inviter_pubkey),
    INDEX idx_invites_expires (expires_at),
    FOREIGN KEY (inviter_pubkey) REFERENCES identities(pubkey) ON DELETE CASCADE
);