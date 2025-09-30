"""
Tests for the user job (protocols.quiet.events.user.job.user_job).

Confirms it emits outgoing peer, user, and address envelopes for
recently joined users and includes transit_secret_id and correct deps.
"""
from __future__ import annotations

import sqlite3
import json
import time

from protocols.quiet.events.user.job import user_job


def _init_minimal_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    # Minimal Event Store
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            event_id   TEXT PRIMARY KEY,
            event_blob BLOB NOT NULL,
            visibility TEXT CHECK(visibility IN ('local-only','network')) DEFAULT 'local-only'
        )
        """
    )
    # Users
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            peer_id TEXT NOT NULL,
            network_id TEXT NOT NULL,
            name TEXT NOT NULL,
            joined_at INTEGER NOT NULL,
            invite_pubkey TEXT NOT NULL,
            UNIQUE(peer_id, network_id)
        )
        """
    )
    # Addresses
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS addresses (
            peer_id TEXT NOT NULL,
            ip TEXT NOT NULL,
            port INTEGER NOT NULL,
            network_id TEXT NOT NULL,
            registered_at_ms INTEGER NOT NULL,
            is_active BOOLEAN DEFAULT TRUE,
            PRIMARY KEY (peer_id, ip, port)
        )
        """
    )
    conn.commit()


def test_user_job_emits_outgoing_for_recent_join() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _init_minimal_schema(conn)
    cur = conn.cursor()

    now_ms = int(time.time() * 1000)

    # Seed a recently joined user
    user_id = "u1"
    peer_id = "p1"
    network_id = "n1"
    cur.execute(
        """
        INSERT INTO users (user_id, peer_id, network_id, name, joined_at, invite_pubkey)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, peer_id, network_id, "Bob", now_ms - 1000, "")
    )

    # Seed an active address for the peer
    dest_ip = "127.0.0.1"
    dest_port = 6101
    cur.execute(
        """
        INSERT INTO addresses (peer_id, ip, port, network_id, registered_at_ms, is_active)
        VALUES (?, ?, ?, ?, ?, 1)
        """,
        (peer_id, dest_ip, dest_port, network_id, now_ms)
    )

    # Seed a local-only transit_secret event (plaintext JSON for the test)
    ts_id = "ts1"
    ts_pt = {
        "type": "transit_secret",
        "unsealed_secret": "00" * 32,
        "network_id": network_id,
        "dest_ip": dest_ip,
        "dest_port": dest_port,
        "created_at": now_ms,
    }
    cur.execute(
        """
        INSERT INTO events (event_id, event_blob, visibility)
        VALUES (?, ?, 'local-only')
        """,
        (ts_id, json.dumps(ts_pt, separators=(",", ":")).encode("utf-8"))
    )

    # Seed stored peer and user event blobs (event-layer encrypted bytes).
    # For user, craft header 0x01 | key_id(16) | nonce(24) | ct to derive key_secret_id.
    key_id = bytes.fromhex("ab" * 16)
    user_blob = b"\x01" + key_id + (b"\x00" * 24) + (b"\xaa" * 8)
    peer_blob = b"peer-bytes-placeholder"

    cur.execute(
        """
        INSERT INTO events (event_id, event_blob, visibility)
        VALUES (?, ?, 'network')
        """,
        (user_id, user_blob)
    )
    cur.execute(
        """
        INSERT INTO events (event_id, event_blob, visibility)
        VALUES (?, ?, 'network')
        """,
        (peer_id, peer_blob)
    )
    conn.commit()

    # Run the job
    ok, state, envelopes = user_job({}, conn, now_ms)
    assert ok is True
    assert isinstance(envelopes, list)
    # Expect at least peer + user; address depends on addresses projection
    types = [e.get("event_type") for e in envelopes]
    assert "peer" in types
    assert "user" in types
    assert "address" in types

    # Validate transit properties on a peer envelope
    penv = next(e for e in envelopes if e.get("event_type") == "peer")
    assert penv["is_outgoing"] is True
    assert penv["network_id"] == network_id
    assert penv["peer_id"] == peer_id
    assert penv["transit_secret_id"] == ts_id
    assert ts_id in (penv.get("deps") or [])
    assert penv["dest_ip"] == dest_ip
    assert penv["dest_port"] == dest_port
    assert penv["event_blob"] == peer_blob

    # Validate transit properties on a user envelope
    uenv = next(e for e in envelopes if e.get("event_type") == "user")
    assert uenv["is_outgoing"] is True
    assert uenv["network_id"] == network_id
    assert uenv["peer_id"] == peer_id
    assert uenv["transit_secret_id"] == ts_id
    assert ts_id in (uenv.get("deps") or [])
    assert uenv["dest_ip"] == dest_ip
    assert uenv["dest_port"] == dest_port
    assert uenv["event_blob"] == user_blob

    # Validate address envelope composition
    aenv = next(e for e in envelopes if e.get("event_type") == "address")
    pt = aenv.get("event_plaintext") or {}
    assert pt.get("type") == "address"
    assert pt.get("peer_id") == peer_id
    assert pt.get("ip") == dest_ip
    assert pt.get("port") == dest_port
    assert pt.get("network_id") == network_id
    assert aenv["peer_id"] == peer_id
    assert aenv["network_id"] == network_id
    assert aenv["is_outgoing"] is True
    assert aenv["transit_secret_id"] == ts_id
    assert ts_id in (aenv.get("deps") or [])
    # key_secret_id should be derived from the user ciphertext header
    assert aenv.get("key_secret_id") == key_id.hex()

