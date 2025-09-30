def execute(params, db):
    """
    Process blocked events in the blocked table.
    
    This job runs periodically to retry processing events that were previously blocked
    due to missing dependencies. It processes events in order of creation time.
    """
    import os
    import json as _json
    time_now_ms = params.get('time_now_ms', 1000)
    cursor = getattr(db, 'conn', None).cursor() if hasattr(db, 'conn') else None

    # Optional: acquire a lightweight lease to avoid multiple drainers
    try:
        if hasattr(db, 'conn') and params.get('time_now_ms') is not None:
            from core.lease import acquire_lease
            now_ms = int(params.get('time_now_ms') or 0)
            # Short TTL; job runs frequently
            acquired = acquire_lease(db.conn, 'signed_groups.blocked.process_unblocked', 'job', now_ms, 2000)
            if not acquired:
                return {"processed": 0}
    except Exception:
        # If lease infra is unavailable, proceed without it
        pass
    
    # Load up to 100 blocked events ordered by creation time
    if cursor is not None:
        rows = cursor.execute(
            """
            SELECT event_id, event_type, data, metadata, blocked_by_id 
            FROM blocked 
            ORDER BY created_at_ms 
            LIMIT 100
            """
        ).fetchall()
    else:
        # No SQL connection; nothing to do
        return {"processed": 0}
    
    # If no blocked events, nothing to do
    if not rows:
        return {"processed": 0}

    # Import handle here to avoid circular imports
    from core.handle import handle
    
    # Try to process each blocked event
    processed = 0
    unblocked_ids = []
    
    for event_id, event_type, data_json, metadata_json, blocked_by_id in rows:
        try:
            # Parse the stored event data
            data = _json.loads(data_json) if data_json else {}
            metadata = _json.loads(metadata_json) if metadata_json else {}
            
            # Create envelope
            envelope = {'payload': data, 'metadata': metadata}
            
            # Try to process the event - this may succeed now if dependency was met
            # Use auto_transaction=False since we're already in a transaction
            handle(db, envelope, time_now_ms=metadata.get('timestamp', time_now_ms), auto_transaction=False)
            
            # If we get here without exception, the event was processed successfully
            unblocked_ids.append(event_id)
            processed += 1
            
            if os.environ.get("TEST_MODE"):
                print(f"[blocked.process_unblocked] Successfully processed blocked event {event_id}")
                
        except Exception as e:
            # Event still can't be processed, leave it blocked
            if os.environ.get("TEST_MODE"):
                print(f"[blocked.process_unblocked] Event {event_id} still blocked: {e}")
            continue
    
    # Remove successfully processed events from blocked table
    if unblocked_ids:
        placeholders = ','.join('?' * len(unblocked_ids))
        try:
            cursor.execute(
                f"DELETE FROM blocked WHERE event_id IN ({placeholders})",
                unblocked_ids
            )
        except Exception:
            pass

    return {"api_response": {"processed": processed}}