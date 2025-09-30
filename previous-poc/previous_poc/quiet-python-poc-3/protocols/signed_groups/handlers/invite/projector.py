def project(db, envelope, time_now_ms):
    """
    Project invite events into SQL with proper blocking for missing dependencies.
    """
    # Import blocking helpers at module level
    import sys
    import os
    sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    from blocked_helper import block_event, unblock
    data = envelope.get('payload', {})
    if data.get('type') != 'invite':
        return db

    invite_id = data.get('id')
    invite_pubkey = data.get('invite_pubkey')
    network_id = data.get('network_id')
    created_by = data.get('created_by')
    group_id = data.get('group_id')
    signature = data.get('signature')
    if not all([invite_id, invite_pubkey, network_id, created_by, signature]):
        return db
    if not group_id:
        # Missing required group_id – drop projection (no dict block state)
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
    try:
        cur = db.conn.cursor()
        # ensure group exists
        g = cur.execute("SELECT 1 FROM groups WHERE id = ? LIMIT 1", (group_id,)).fetchone()
        if not g:
            block_event(db, envelope, group_id, f"Group {group_id} not found")
            return db
            
        # ensure creator exists and get pubkey
        creator_row = cur.execute("SELECT pubkey FROM users WHERE id = ? LIMIT 1", (created_by,)).fetchone()
        if not creator_row:
            block_event(db, envelope, created_by, f"Invite creator {created_by} not found")
            return db
            
        # Import crypto module
        import sys
        import os
        sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
        from core import crypto
        
        creator_pubkey = creator_row[0]
        
        # Verify signature
        sig_data = f"invite:{invite_id}:{invite_pubkey}:{network_id}:{group_id}:{created_by}"
        if not crypto.verify(sig_data, signature, creator_pubkey):
            # Known user but invalid signature - just drop the event
            return db
            
        # Enforce first-group rule using networks.first_group_id
        n = cur.execute("SELECT first_group_id FROM networks WHERE id = ? LIMIT 1", (network_id,)).fetchone()
        current_first = n[0] if n else None
        first_group_ok = True
        if current_first:
            first_group_ok = (current_first == group_id)
        else:
            cur.execute(
                "INSERT OR IGNORE INTO networks(id, name, creator_pubkey, first_group_id, created_at_ms) VALUES(?, ?, ?, ?, 0)",
                (network_id, '', created_by or '', group_id)
            )
            cur.execute("UPDATE networks SET first_group_id = COALESCE(first_group_id, ?) WHERE id = ?", (group_id, network_id))
            
        if not first_group_ok:
            # This invite is for the wrong group
            block_event(db, envelope, f"first_group_{group_id}", f"Must create invite for first group: {current_first}")
            return db
    except Exception:
        return db

    # Persist invite
    try:
        if hasattr(db, 'conn'):
            cur = db.conn.cursor()
            cur.execute(
                """
                INSERT OR IGNORE INTO invites(id, invite_pubkey, network_id, group_id, created_by, created_at_ms)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (invite_id, invite_pubkey, network_id, group_id, created_by, int(time_now_ms or 0))
            )
            # Commit is managed by the framework transaction
    except Exception:
        pass

    # Unblock any events waiting for this invite
    try:
        unblock(db, invite_id)
    except Exception:
        pass

    return db
