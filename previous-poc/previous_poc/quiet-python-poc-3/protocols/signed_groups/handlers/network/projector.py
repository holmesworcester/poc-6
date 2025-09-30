def project(db, envelope, time_now_ms):
    """
    Project network events into SQL (dict-state deprecated).
    """
    data = envelope.get('payload', {})
    if data.get('type') != 'network':
        return db

    network_id = data.get('id')
    name = data.get('name')
    creator_pubkey = data.get('creator_pubkey')
    if not network_id or not name or not creator_pubkey:
        return db

    # Append to event_store
    try:
        import sys
        import os
        # Add parent directory to path to allow imports
        handler_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if handler_dir not in sys.path:
            sys.path.insert(0, handler_dir)
        from event_store.append import append as _append_event
        _append_event(db, envelope, time_now_ms)
    except Exception:
        pass

    # Persist to SQL
    try:
        if hasattr(db, 'conn'):
            cur = db.conn.cursor()
            cur.execute(
                """
                INSERT OR IGNORE INTO networks(id, name, creator_pubkey, created_at_ms)
                VALUES(?, ?, ?, ?)
                """,
                (network_id, name, creator_pubkey, int(time_now_ms or 0))
            )
            # Commit is managed by the framework transaction
    except Exception:
        pass

    return db
