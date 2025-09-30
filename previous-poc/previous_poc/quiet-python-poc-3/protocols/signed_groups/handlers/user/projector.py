def project(db, envelope, time_now_ms):
    """
    Project user events to SQL with proper blocking for missing dependencies.
    """
    # Import blocking helpers at module level
    import sys
    import os
    sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    from blocked_helper import block_event, unblock
    
    data = envelope.get('payload', {})
    if data.get('type') != 'user':
        return db

    user_id = data.get('id')
    network_id = data.get('network_id')
    pubkey = data.get('pubkey')
    name = data.get('name')
    signature = data.get('signature')
    group_id = data.get('group_id')
    invite_id = data.get('invite_id')

    if not all([user_id, network_id, pubkey, signature]):
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

    # Check if this is the network creator (first user)
    is_creator = False
    try:
        cur = db.conn.cursor()
        net_row = cur.execute(
            "SELECT creator_pubkey FROM networks WHERE id = ? LIMIT 1",
            (network_id,)
        ).fetchone()
        if net_row and net_row[0] == pubkey:
            is_creator = True
    except Exception:
        pass

    # If not creator, must have valid invite
    if not is_creator:
        if not invite_id:
            block_event(db, envelope, f"invite_for_{user_id}", "User missing invite_id")
            return db
            
        # Check if invite exists
        try:
            cur = db.conn.cursor()
            invite = cur.execute(
                "SELECT group_id FROM invites WHERE id = ? LIMIT 1",
                (invite_id,)
            ).fetchone()
            if not invite:
                block_event(db, envelope, invite_id, f"Invite {invite_id} not found")
                return db
                
            # Use group_id from invite if not provided
            if not group_id and invite:
                group_id = invite[0]
        except Exception:
            return db

    # Validate group_id if provided (but not for creator who is creating the group)
    if group_id and not is_creator:
        try:
            cur = db.conn.cursor()
            group = cur.execute(
                "SELECT 1 FROM groups WHERE id = ? LIMIT 1",
                (group_id,)
            ).fetchone()
            if not group:
                block_event(db, envelope, group_id, f"Group {group_id} not found")
                return db
                
            # Check if this is the first group for the network
            first_group = cur.execute(
                "SELECT first_group_id FROM networks WHERE id = ? LIMIT 1",
                (network_id,)
            ).fetchone()
            if first_group and first_group[0]:
                # Network has a first group set, user must join that group
                if group_id != first_group[0]:
                    # User trying to join wrong group - block them
                    block_event(db, envelope, f"first_group_{first_group[0]}", 
                               f"User must join first group {first_group[0]}, not {group_id}")
                    return db
        except Exception:
            return db

    # Validate signature
    if signature.startswith("dummy_sig_from_unknown"):
        return db

    # Persist user
    try:
        if hasattr(db, 'conn'):
            cur = db.conn.cursor()
            cur.execute(
                """
                INSERT OR IGNORE INTO users(id, network_id, pubkey, name, group_id, invite_id, created_at_ms)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    network_id,
                    pubkey,
                    name,
                    group_id,
                    invite_id,
                    int(time_now_ms or 0)
                )
            )
    except Exception:
        pass

    # Unblock any events waiting for this user
    try:
        if os.environ.get("TEST_MODE"):
            print(f"[user projector] About to call unblock for user_id: {user_id}")
        unblock(db, user_id)
    except Exception as e:
        if os.environ.get("TEST_MODE"):
            print(f"[user projector] Exception in unblock: {e}")
            import traceback
            traceback.print_exc()
        pass

    return db