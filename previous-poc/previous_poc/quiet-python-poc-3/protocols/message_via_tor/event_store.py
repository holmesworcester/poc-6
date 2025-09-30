"""
Event store helper for message_via_tor protocol.
Handles persisting events to the event_store table with message_via_tor's schema.
"""
import json
import uuid


def append(db, envelope, time_now_ms):
    """
    Append an event envelope to the message_via_tor event_store.
    
    message_via_tor schema:
    - pubkey: The identity that owns/received this event
    - event_data: JSON of the event payload
    - metadata: JSON of the event metadata
    - event_type: Type of the event
    - event_id: Unique event identifier
    - created_at: Timestamp in milliseconds
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
    if not event_id:
        event_id = str(uuid.uuid4())
    
    # Get pubkey - try multiple sources
    pubkey = (
        metadata.get('received_by') or 
        payload.get('sender') or 
        payload.get('pubkey') or 
        'unknown'
    )
    
    try:
        cur = db.conn.cursor()
        cur.execute(
            """
            INSERT OR IGNORE INTO event_store(pubkey, event_data, metadata, event_type, event_id, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                pubkey,
                json.dumps(payload, sort_keys=True),
                json.dumps(metadata, sort_keys=True),
                event_type,
                event_id,
                int(time_now_ms),
            )
        )
        # Commit is managed by the framework transaction
    except Exception as e:
        # Log errors for debugging but don't crash
        import traceback
        print(f"[message_via_tor] Failed to store event: {e}")
        traceback.print_exc()