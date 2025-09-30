def project(db, envelope, time_now_ms):
    """
    Project add events into SQL with proper blocking for missing dependencies.
    """
    # Import blocking helpers at module level
    import sys
    import os
    sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    from blocked_helper import block_event, unblock
    
    data = envelope.get('payload', {})
    if data.get('type') != 'add':
        return db

    add_id = data.get('id')
    group_id = data.get('group_id')
    user_id = data.get('user_id')
    added_by = data.get('added_by')
    signature = data.get('signature')
    if not all([add_id, group_id, user_id, added_by, signature]):
        return db
    
    if os.environ.get("TEST_MODE"):
        print(f"[add projector] Processing add {add_id}: user {user_id} to group {group_id} by {added_by}")

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

    # Minimal validation via SQL only
    try:
        cur = db.conn.cursor()
        
        # Check if group exists
        g = cur.execute("SELECT 1 FROM groups WHERE id = ? LIMIT 1", (group_id,)).fetchone()
        if not g:
            if os.environ.get("TEST_MODE"):
                print(f"[add projector] Blocking: Group {group_id} not found")
            block_event(db, envelope, group_id, f"Group {group_id} not found")
            return db
            
        # Check if user to be added exists
        u1 = cur.execute("SELECT 1 FROM users WHERE id = ? LIMIT 1", (user_id,)).fetchone()
        if not u1:
            if os.environ.get("TEST_MODE"):
                print(f"[add projector] Blocking: User {user_id} not found")
            block_event(db, envelope, user_id, f"User {user_id} not found")
            return db
            
        # Check if adder exists and get pubkey
        adder_row = cur.execute("SELECT pubkey FROM users WHERE id = ? LIMIT 1", (added_by,)).fetchone()
        if not adder_row:
            block_event(db, envelope, added_by, f"Adder {added_by} not found")
            return db
            
        # Import crypto module
        import sys
        import os
        sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
        from core import crypto
        
        adder_pubkey = adder_row[0]
        
        # Verify signature
        sig_data = f"add:{add_id}:{group_id}:{user_id}:{added_by}"
        if not crypto.verify(sig_data, signature, adder_pubkey):
            block_event(db, envelope, f"valid_sig_for_{add_id}", "Invalid add signature")
            return db
    except Exception:
        return db

    # Persist to SQL
    try:
        if hasattr(db, 'conn'):
            cur = db.conn.cursor()
            cur.execute(
                """
                INSERT OR IGNORE INTO adds(id, group_id, user_id, added_by, created_at_ms)
                VALUES(?, ?, ?, ?, ?)
                """,
                (add_id, group_id, user_id, added_by, int(time_now_ms or 0))
            )
            if os.environ.get("TEST_MODE"):
                print(f"[add projector] Successfully added user {user_id} to group {group_id}")
            # Commit is managed by the framework transaction
    except Exception as e:
        if os.environ.get("TEST_MODE"):
            print(f"[add projector] Failed to persist add: {e}")
        pass

    # Unblock any events waiting for this add (e.g., messages from this user in group channels)
    try:
        unblock(db, add_id)
        # Also unblock anything waiting for this specific group membership
        unblock(db, f"group_membership_{group_id}_{user_id}")
    except Exception:
        pass

    return db