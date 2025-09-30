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
    """Use the protocol's event_store helper if available, otherwise use local implementation."""
    try:
        # Try to import the protocol's event_store helper dynamically
        import sys
        import os
        protocol_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        sys.path.insert(0, protocol_root)
        from event_store import append
        return append(db, envelope, time_now_ms)
    except ImportError:
        # Fall back to local implementation
        if not hasattr(db, 'conn') or db.conn is None:
            return
        data = envelope.get('payload', {})
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
    Project identity events into SQL-owned tables (no dict-state).
    """
    # Get data from envelope (support both 'data' and 'payload' fields)
    data = envelope.get('payload', {})
    if data.get('type') != 'identity':
        return db

    pubkey = data.get('pubkey')
    privkey = data.get('privkey')
    name = data.get('name') or (pubkey[:8] if pubkey else None)
    if not pubkey or not privkey:
        return db
    
    # Debug
    import os
    if os.environ.get('TEST_MODE'):
        print(f"[identity.projector] Processing identity event: pubkey={pubkey[:16]}..., name={name}")

    # Persist to SQL (protocol-owned table)
    try:
        if hasattr(db, 'conn'):
            cur = db.conn.cursor()
            cur.execute(
                """
                INSERT INTO identities(pubkey, privkey, name, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(pubkey) DO UPDATE SET
                    name=excluded.name,
                    updated_at=excluded.updated_at
                """,
                (pubkey, privkey, name, int(time_now_ms or 0), int(time_now_ms or 0))
            )
            # Append to SQL event_store (protocol-owned)
            _append_event(db, envelope, time_now_ms)
    except Exception as e:
        try:
            import os
            if os.environ.get('TEST_MODE'):
                print(f"[identity.projector] SQL error: {e}")
        except Exception:
            pass
        # Keep projection resilient if SQL not available or schema mismatch
        pass

    return db
