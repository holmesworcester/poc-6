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
        import importlib.util
        protocol_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        event_store_path = os.path.join(protocol_root, 'event_store.py')
        spec = importlib.util.spec_from_file_location("event_store_module", event_store_path)
        event_store_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(event_store_module)
        return event_store_module.append(db, envelope, time_now_ms)
    except ImportError:
        # Fall back to local implementation
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
    Project message events to SQL tables only (no dict-state).
    Marks unknown_peer when sender is not known to the recipient.
    """
    # Get data and metadata
    data = envelope.get('payload', {})
    metadata = envelope.get('metadata', {})
    sender = data.get('sender') or metadata.get('sender')

    # Append to SQL event_store (protocol-owned)
    _append_event(db, envelope, time_now_ms)

    # Require text
    text = data.get('text')
    if not text:
        return db

    # Determine received_by
    received_by = metadata.get('received_by')
    if not received_by and metadata.get('selfGenerated'):
        received_by = sender
    # If still missing, fall back to sender to satisfy NOT NULL constraint
    if not received_by:
        received_by = sender

    # Check if sender is known to the recipient via SQL
    is_known_peer = False
    if received_by and hasattr(db, 'conn'):
        try:
            cur = db.conn.cursor()
            row = cur.execute(
                "SELECT 1 FROM peers WHERE pubkey = ? AND received_by = ? LIMIT 1",
                (sender, received_by)
            ).fetchone()
            is_known_peer = bool(row)
        except Exception:
            is_known_peer = False

    # Persist to SQL
    try:
        if hasattr(db, 'conn'):
            # Ensure event_id
            event_id = metadata.get('eventId')
            if not event_id:
                try:
                    import uuid as _uuid
                    event_id = str(_uuid.uuid4())
                    # reflect back into envelope for consistency
                    metadata['eventId'] = event_id
                except Exception:
                    event_id = None
            if received_by and event_id:
                cur = db.conn.cursor()
                cur.execute(
                    """
                    INSERT OR IGNORE INTO messages (
                        event_id, text, sender, recipient, received_by, timestamp, sig, unknown_peer, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        text,
                        sender,
                        data.get('recipient'),
                        received_by,
                        int(data.get('timestamp') or time_now_ms or 0),
                        (data.get('sig') or ''),
                        0 if is_known_peer else 1,
                        int(time_now_ms or 0)
                    )
                )
                # Commit is managed by framework transaction
    except Exception as e:
        try:
            import os
            if os.environ.get('TEST_MODE'):
                print(f"[message.projector] SQL insert failed: {e}")
        except Exception:
            pass
        # Keep projection resilient if SQL not available or schema mismatch
        pass

    return db
