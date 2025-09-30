def project(db, envelope, time_now_ms):
    """
    Project channel events into SQL with proper blocking for missing dependencies.
    """
    # Import blocking helpers at module level
    import sys
    import os
    sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    from blocked_helper import block_event, unblock
    data = envelope.get('payload', {})
    if data.get('type') != 'channel':
        return db

    channel_id = data.get('id')
    network_id = data.get('network_id')
    name = data.get('name')
    created_by = data.get('created_by') or data.get('user_id')  # Support both field names
    group_id = data.get('group_id')
    signature = data.get('signature')
    if not all([channel_id, network_id, name, created_by, signature]):
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

    # Validate dependencies
    if hasattr(db, 'conn'):
        try:
            cur = db.conn.cursor()
            # Check creator exists
            creator_row = cur.execute("SELECT pubkey FROM users WHERE id = ? LIMIT 1", (created_by,)).fetchone()
            if not creator_row:
                block_event(db, envelope, created_by, f"Channel creator {created_by} not found")
                return db
                
            # Import crypto module
            import sys
            import os
            sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
            from core import crypto
            
            creator_pubkey = creator_row[0]
            
            # Verify signature
            sig_data = f"channel:{channel_id}:{name}:{network_id}:{created_by}"
            if group_id:
                sig_data += f":{group_id}"
            if not crypto.verify(sig_data, signature, creator_pubkey):
                block_event(db, envelope, f"valid_sig_for_{channel_id}", "Invalid channel signature")
                return db
                
            # Enforce existing group if provided
            if group_id:
                g = cur.execute("SELECT 1 FROM groups WHERE id = ? LIMIT 1", (group_id,)).fetchone()
                if not g:
                    block_event(db, envelope, group_id, f"Group {group_id} not found")
                    return db
        except Exception:
            return db

    # Persist to SQL
    try:
        if hasattr(db, 'conn'):
            cur = db.conn.cursor()
            cur.execute(
                """
                INSERT OR IGNORE INTO channels(id, network_id, name, created_by, group_id, created_at_ms)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (channel_id, network_id, name, created_by, group_id, int(time_now_ms or 0))
            )
            # Commit is managed by the framework transaction
    except Exception:
        pass

    # Unblock any events waiting for this channel
    try:
        unblock(db, channel_id)
    except Exception:
        pass

    return db
