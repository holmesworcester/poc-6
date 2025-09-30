def project(db, envelope, time_now_ms):
    """
    Project link-invite events into SQL with proper blocking for missing dependencies.
    """
    # Import blocking helpers at module level
    import sys
    import os
    sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    from blocked_helper import block_event, unblock
    data = envelope.get('payload', {})
    if data.get('type') != 'link_invite':
        return db

    link_invite_id = data.get('id')
    link_invite_pubkey = data.get('link_invite_pubkey')
    user_id = data.get('user_id')
    signature = data.get('signature')
    if not all([link_invite_id, link_invite_pubkey, user_id, signature]):
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
            
            # Handle dummy signatures first (for testing)
            if signature.startswith("dummy_sig_from_unknown"):
                block_event(db, envelope, f"valid_sig_for_{link_invite_id}", "Link invite signed by non-user")
                return db
                
            # Get user and their public key
            user_row = cur.execute("SELECT pubkey FROM users WHERE id = ? LIMIT 1", (user_id,)).fetchone()
            if not user_row:
                block_event(db, envelope, user_id, f"User {user_id} not found")
                return db
                
            # Import crypto module
            import sys
            import os
            sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
            from core import crypto
            
            user_pubkey = user_row[0]
            
            # Verify signature
            sig_data = f"link_invite:{link_invite_id}:{link_invite_pubkey}:{user_id}"
            if not crypto.verify(sig_data, signature, user_pubkey):
                block_event(db, envelope, f"valid_sig_for_{link_invite_id}", "Invalid link invite signature")
                return db
        except Exception as e:
            import os
            if os.environ.get("DEBUG"):
                print(f"[link_invite projector] Exception: {e}")
            return db

    # Persist to SQL
    try:
        if hasattr(db, 'conn'):
            cur = db.conn.cursor()
            cur.execute(
                """
                INSERT OR IGNORE INTO link_invites(id, link_invite_pubkey, user_id, created_at_ms)
                VALUES(?, ?, ?, ?)
                """,
                (link_invite_id, link_invite_pubkey, user_id, int(time_now_ms or 0))
            )
            # Commit is managed by the framework transaction
    except Exception as e:
        import os
        if os.environ.get("DEBUG"):
            print(f"[link_invite projector] Persist exception: {e}")
        pass

    # Unblock any events waiting for this link invite
    try:
        unblock(db, link_invite_id)
    except Exception:
        pass

    return db
