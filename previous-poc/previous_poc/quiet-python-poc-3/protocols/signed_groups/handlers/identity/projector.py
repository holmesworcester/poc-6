def project(db, envelope, time_now_ms):
    """
    Project identity events to SQL (dict-state deprecated).
    """
    data = envelope.get('payload', {})
    if data.get('type') != 'identity':
        return db

    pubkey = data.get('pubkey')
    privkey = data.get('privkey')
    name = data.get('name') or (pubkey[:8] if pubkey else None)
    if not pubkey or not privkey:
        return db

    # Persist to SQL
    try:
        if hasattr(db, 'conn'):
            cur = db.conn.cursor()
            cur.execute(
                """
                INSERT OR IGNORE INTO identities(pubkey, privkey, name, created_at_ms)
                VALUES(?, ?, ?, ?)
                """,
                (pubkey, privkey, name, int(time_now_ms or 0))
            )
    except Exception:
        pass

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

    return db
