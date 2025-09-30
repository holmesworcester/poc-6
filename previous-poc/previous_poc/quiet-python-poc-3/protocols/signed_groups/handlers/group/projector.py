def project(db, envelope, time_now_ms):
    """
    Project group events to SQL with proper blocking.
    """
    # Import blocking helpers at module level
    import sys
    import os
    sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    from blocked_helper import block_event, unblock
    data = envelope.get('payload', {})
    if data.get('type') != 'group':
        return db

    group_id = data.get('id')
    name = data.get('name')
    created_by = data.get('created_by')
    signature = data.get('signature')
    if not all([group_id, name, created_by, signature]):
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

    # Validate creator exists
    try:
        cur = db.conn.cursor()
        u = cur.execute("SELECT 1 FROM users WHERE id = ? LIMIT 1", (created_by,)).fetchone()
        if not u:
            block_event(db, envelope, created_by, f"Group creator {created_by} not found")
            return db
            
        # Import crypto module
        import sys
        import os
        sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
        from core import crypto
        
        # Get creator's public key for verification
        creator_row = cur.execute("SELECT pubkey FROM users WHERE id = ?", (created_by,)).fetchone()
        if not creator_row:
            block_event(db, envelope, f"pubkey_for_{created_by}", f"Public key not found for creator {created_by}")
            return db
        
        creator_pubkey = creator_row[0]
        
        # Verify signature
        sig_data = f"group:{group_id}:{name}:{created_by}"
        if not crypto.verify(sig_data, signature, creator_pubkey):
            block_event(db, envelope, f"valid_sig_for_{group_id}", "Invalid group signature")
            return db
    except Exception:
        return db

    # Persist to SQL
    try:
        if hasattr(db, 'conn'):
            cur = db.conn.cursor()
            cur.execute(
                """
                INSERT OR IGNORE INTO groups(id, name, created_by, created_at_ms)
                VALUES(?, ?, ?, ?)
                """,
                (group_id, name, created_by, int(time_now_ms or 0))
            )
            # Commit is managed by the framework transaction
    except Exception:
        pass

    # Unblock any events waiting for this group
    try:
        unblock(db, group_id)
    except Exception:
        pass

    return db