import json
import uuid


def _ensure_event_id(metadata):
    if not isinstance(metadata, dict):
        return str(uuid.uuid4())
    eid = metadata.get('eventId')
    if not eid:
        eid = str(uuid.uuid4())
        metadata['eventId'] = eid
    return eid


def _append_event(db, envelope, time_now_ms):
    if not hasattr(db, 'conn') or db.conn is None:
        return
    data = envelope.get('payload') or {}
    metadata = envelope.get('metadata') or {}
    event_type = data.get('type') or 'unknown'
    event_id = _ensure_event_id(metadata)
    pubkey = metadata.get('received_by') or data.get('sender') or data.get('pubkey') or 'unknown'
    try:
        cur = db.conn.cursor()
        cur.execute(
            """
            INSERT OR IGNORE INTO event_store(pubkey, event_data, metadata, event_type, event_id, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                pubkey,
                json.dumps(data, sort_keys=True),
                json.dumps(metadata, sort_keys=True),
                event_type,
                event_id,
                int(time_now_ms or 0),
            )
        )
    except Exception:
        pass


def project(db, envelope, time_now_ms):
    """
    Validate and persist peer events to SQL (no dict-state).
    Tracks which local identity knows this peer via metadata.received_by.
    """
    # Get data and metadata from envelope
    data = envelope.get('payload', {})
    metadata = envelope.get('metadata', {})
    if data.get('type') != 'peer':
        return db

    pubkey = data.get('pubkey')
    if not pubkey:
        return db

    # Determine which identity received this peer event
    # Allow received_by from metadata or from data (legacy tests)
    received_by = metadata.get('received_by') or data.get('received_by')
    if not received_by:
        # If self-generated and peer is our identity, default to that pubkey
        if metadata.get('selfGenerated') and pubkey:
            received_by = pubkey
        # If this peer matches a local identity, default to that identity
        if not received_by and hasattr(db, 'conn') and pubkey:
            try:
                cur = db.conn.cursor()
                row = cur.execute("SELECT 1 FROM identities WHERE pubkey = ? LIMIT 1", (pubkey,)).fetchone()
                if row:
                    received_by = pubkey
            except Exception:
                pass
        if not received_by:
            return db

    name = data.get('name', pubkey[:8])
    joined_via = data.get('joined_via', 'direct')

    # Persist to SQL and clear unknown flags on matching messages
    try:
        if hasattr(db, 'conn'):
            cur = db.conn.cursor()
            # Insert peer knowledge record
            cur.execute(
                """
                INSERT OR IGNORE INTO peers(pubkey, name, joined_via, added_at, received_by)
                VALUES(?, ?, ?, ?, ?)
                """,
                (pubkey, name, joined_via, int(time_now_ms or 0), received_by)
            )
            # Clear unknown_peer on messages for this sender/recipient pair
            try:
                cur.execute(
                    "UPDATE messages SET unknown_peer = 0 WHERE sender = ? AND received_by = ?",
                    (pubkey, received_by)
                )
            except Exception:
                pass
            # Append to SQL event_store (protocol-owned)
            _append_event(db, envelope, time_now_ms)
    except Exception as e:
        try:
            import os
            if os.environ.get('TEST_MODE'):
                print(f"[peer.projector] SQL error: {e}")
        except Exception:
            pass
        # Keep projection resilient if SQL not available or schema mismatch
        pass

    return db
