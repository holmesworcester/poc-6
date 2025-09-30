"""
Unblock events that were waiting for a specific dependency.
This is called by projectors after successfully projecting an event.
"""
import json


def unblock(db, event_id):
    """
    Find and process any events that were blocked waiting for this event_id.
    
    Args:
        db: Database connection
        event_id: The ID of the event that just arrived (e.g. user_id, group_id)
    """
    print(f"[unblock] Called with event_id: {event_id}")
    if not hasattr(db, 'conn'):
        return
    
    cursor = db.conn.cursor()
    
    # Find all events blocked by this event_id
    blocked_events = cursor.execute(
        """
        SELECT event_id, event_type, data, metadata 
        FROM blocked 
        WHERE blocked_by_id = ?
        ORDER BY created_at_ms
        """,
        (event_id,)
    ).fetchall()
    
    print(f"[unblock] Found {len(blocked_events)} blocked events waiting for {event_id}")
    if not blocked_events:
        return
    
    # Import handle here to avoid circular imports
    from core.handle import handle
    
    # Process each blocked event
    unblocked_ids = []
    for event_id, event_type, data_json, metadata_json in blocked_events:
        try:
            # Parse the stored event data
            data = json.loads(data_json) if data_json else {}
            metadata = json.loads(metadata_json) if metadata_json else {}
            
            # Create envelope and try to process it
            envelope = {'payload': data, 'metadata': metadata}
            
            # Process the event - this may succeed now that dependency is met
            # Note: We use auto_transaction=False because we're already in a transaction
            # from the parent projector, but we need to ensure changes are persisted
            handle(db, envelope, time_now_ms=metadata.get('timestamp', 0), auto_transaction=False)
            
            # If we get here, the event was processed successfully
            unblocked_ids.append(event_id)
            
        except Exception:
            # Event still can't be processed, leave it blocked
            pass
    
    # Remove successfully processed events from blocked table
    if unblocked_ids:
        placeholders = ','.join('?' * len(unblocked_ids))
        cursor.execute(
            f"DELETE FROM blocked WHERE event_id IN ({placeholders})",
            unblocked_ids
        )


def block_event(db, envelope, blocked_by_id, reason):
    """
    Add an event to the blocked table.
    
    Args:
        db: Database connection
        envelope: The event envelope to block
        blocked_by_id: The ID of the missing dependency
        reason: Human-readable reason for blocking
    """
    if not hasattr(db, 'conn'):
        return
        
    cursor = db.conn.cursor()
    data = envelope.get('payload', {})
    event_id = data.get('id', '')
    event_type = data.get('type', '')
    
    if not event_id:
        return
    
    try:
        cursor.execute(
            """
            INSERT OR IGNORE INTO blocked (event_id, blocked_by_id, event_type, data, metadata, reason, created_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                blocked_by_id,
                event_type,
                json.dumps(data),
                json.dumps(envelope.get('metadata', {})),
                reason,
                int(envelope.get('metadata', {}).get('timestamp', 0))
            )
        )
    except Exception:
        pass