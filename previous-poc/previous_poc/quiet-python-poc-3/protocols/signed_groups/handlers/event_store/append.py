"""Helper function for appending events to the protocol's event store."""
import json
import uuid


def append(db, envelope, time_now_ms):
    """
    Append an event envelope to the SQL event_store.
    Uses the standard envelope format with 'payload' and 'metadata'.
    """
    if not hasattr(db, 'conn') or db.conn is None:
        return
    
    # Default time if not provided
    if not time_now_ms:
        import time
        time_now_ms = int(time.time() * 1000)
    
    # Extract payload and metadata from standard envelope
    payload = envelope.get('payload', {})
    metadata = envelope.get('metadata', {})
    
    # Get event type from payload
    event_type = payload.get('type', 'unknown')
    
    # Get or generate event ID
    event_id = metadata.get('eventId')
    if not event_id and isinstance(payload, dict) and payload.get('id'):
        event_id = str(payload['id'])
    if not event_id:
        event_id = str(uuid.uuid4())
    
    try:
        cur = db.conn.cursor()
        cur.execute(
            """
            INSERT OR IGNORE INTO event_store(event_id, event_type, data, metadata, created_at_ms)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                event_id,
                event_type,
                json.dumps(payload, sort_keys=True),
                json.dumps(metadata, sort_keys=True),
                int(time_now_ms or 0),
            )
        )
        # Commit is managed by the framework transaction
    except Exception as e:
        import os
        if os.environ.get('DEBUG'):
            print(f"[DEBUG] event_store.append failed: {e}")
            import traceback
            traceback.print_exc()