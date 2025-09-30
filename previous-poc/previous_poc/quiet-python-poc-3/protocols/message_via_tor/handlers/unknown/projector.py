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
    Persist decrypted but unrecognized events to SQL unknown_events.
    """
    # Build entry
    import json as _json
    unknown_entry = {
        'data': envelope.get('payload'),
        'metadata': envelope.get('metadata', {}),
        'timestamp': envelope.get('metadata', {}).get('receivedAt', time_now_ms)
    }

    # Persist to SQL if available
    try:
        if hasattr(db, 'conn'):
            cur = db.conn.cursor()
            # For idempotency, check if this exact event already exists
            data_str = _json.dumps(unknown_entry['data'], sort_keys=True)
            metadata_str = _json.dumps(unknown_entry['metadata'], sort_keys=True)
            timestamp = int(unknown_entry['timestamp'] or 0)
            
            # Check if already exists
            existing = cur.execute(
                "SELECT 1 FROM unknown_events WHERE data = ? AND metadata = ? AND timestamp = ? LIMIT 1",
                (data_str, metadata_str, timestamp)
            ).fetchone()
            
            if not existing:
                cur.execute(
                    "INSERT INTO unknown_events(data, metadata, timestamp) VALUES(?, ?, ?)",
                    (data_str, metadata_str, timestamp)
                )
            # Append to SQL event_store (protocol-owned)
            _append_event(db, envelope, time_now_ms)
    except Exception:
        pass

    return db
