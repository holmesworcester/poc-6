def project(db, envelope, time_now_ms):
    """
    Project message events to SQL with proper blocking and group membership validation.
    """
    # Import blocking helpers at module level
    import sys
    import os
    sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    from blocked_helper import block_event, unblock
    data = envelope.get('payload', {})
    if data.get('type') != 'message':
        return db

    message_id = data.get('id')
    channel_id = data.get('channel_id')
    author_id = data.get('author_id')
    peer_id = data.get('peer_id')
    user_id = data.get('user_id')
    content = data.get('content') or data.get('text')
    signature = data.get('signature')
    
    if os.environ.get("TEST_MODE"):
        print(f"[message projector] Processing message {message_id} from {author_id}")
    
    if not all([message_id, channel_id, author_id, content, signature, peer_id]):
        if os.environ.get("TEST_MODE"):
            print(f"[message projector] Missing required fields")
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

    # Check if author/user exists
    try:
        cur = db.conn.cursor()
        user = cur.execute("SELECT 1 FROM users WHERE id = ? LIMIT 1", (author_id,)).fetchone()
        if not user:
            block_event(db, envelope, author_id, f"Author {author_id} not found")
            return db
    except Exception as e:
        if os.environ.get("TEST_MODE"):
            print(f"[message projector] Exception checking user: {e}")
        return db

    # Signature validation
    try:
        # Import crypto module
        import sys
        import os
        sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
        from core import crypto
        
        # Get peer's public key for verification
        cur = db.conn.cursor()
        if peer_id == user_id:
            # Self-message: get user's pubkey
            pubkey_row = cur.execute("SELECT pubkey FROM users WHERE id = ?", (user_id,)).fetchone()
        else:
            # Linked device: get peer's pubkey from links
            pubkey_row = cur.execute("SELECT peer_pubkey FROM links WHERE peer_id = ?", (peer_id,)).fetchone()
        
        if not pubkey_row:
            if os.environ.get("TEST_MODE"):
                print(f"[message projector] No pubkey found for peer {peer_id}")
            block_event(db, envelope, f"pubkey_for_{peer_id}", f"Public key not found for peer {peer_id}")
            return db
        
        peer_pubkey = pubkey_row[0]
        if os.environ.get("TEST_MODE"):
            print(f"[message projector] Got pubkey {peer_pubkey} for peer {peer_id}")
        
        # Verify signature
        sig_data = f"message:{message_id}:{channel_id}:{author_id}:{peer_id}:{content}"
        if os.environ.get("TEST_MODE"):
            print(f"[message projector] Verifying signature: {signature}")
            print(f"[message projector] Sig data: {sig_data}")
        if not crypto.verify(sig_data, signature, peer_pubkey):
            if os.environ.get("TEST_MODE"):
                print(f"[message projector] Signature verification failed")
            block_event(db, envelope, f"valid_sig_for_{message_id}", "Invalid message signature")
            return db
            
        # Link check (allow self-messages where peer_id == user_id)
        if peer_id and user_id and peer_id != user_id:
            try:
                cur = db.conn.cursor()
                row = cur.execute(
                    "SELECT 1 FROM links WHERE peer_id = ? AND user_id = ? LIMIT 1",
                    (peer_id, user_id)
                ).fetchone()
                if not row:
                    return db
            except Exception:
                return db
    except Exception as e:
        if os.environ.get("TEST_MODE"):
            print(f"[message projector] Exception in signature validation: {e}")
        return db
    
    if os.environ.get("TEST_MODE"):
        print(f"[message projector] Passed signature validation")

    # Check channel exists and get group_id
    try:
        cur = db.conn.cursor()
        channel = cur.execute(
            "SELECT group_id FROM channels WHERE id = ? LIMIT 1",
            (channel_id,)
        ).fetchone()
        if not channel:
            block_event(db, envelope, channel_id, f"Channel {channel_id} not found")
            return db
        
        group_id = channel[0]
        
        # Check if author is member of the group (if group_id is set)
        if group_id:
            if os.environ.get("TEST_MODE"):
                print(f"[message projector] Checking if {author_id} is member of group {group_id}")
            
            # Check if author is the group creator
            is_creator = cur.execute(
                "SELECT 1 FROM groups WHERE id = ? AND created_by = ? LIMIT 1",
                (group_id, author_id)
            ).fetchone()
            
            if not is_creator:
                # Check if author was added to the group
                is_member = cur.execute(
                    "SELECT 1 FROM adds WHERE group_id = ? AND user_id = ? LIMIT 1",
                    (group_id, author_id)
                ).fetchone()
                
                if os.environ.get("TEST_MODE"):
                    print(f"[message projector] Checked adds table: {'found' if is_member else 'not found'}")
                
                # Also check if user has this group_id set (for users who joined via invite)
                if not is_member:
                    is_member = cur.execute(
                        "SELECT 1 FROM users WHERE id = ? AND group_id = ? LIMIT 1",
                        (author_id, group_id)
                    ).fetchone()
                    if os.environ.get("TEST_MODE"):
                        print(f"[message projector] Checked users table: {'found' if is_member else 'not found'}")
                
                if not is_member:
                    # Block message - author is not in the group
                    block_event(db, envelope, f"group_membership_{group_id}_{author_id}", 
                               f"Author {author_id} is not a member of group {group_id}")
                    return db
    except Exception as e:
        if os.environ.get("TEST_MODE"):
            print(f"[message projector] Exception in group check: {e}")
        return db
    
    if os.environ.get("TEST_MODE"):
        print(f"[message projector] Passed all checks, persisting message")

    # Persist to SQL
    try:
        if hasattr(db, 'conn'):
            cur = db.conn.cursor()
            # Check if message already exists for idempotency
            existing = cur.execute("SELECT 1 FROM messages WHERE id = ? LIMIT 1", (message_id,)).fetchone()
            if not existing:
                cur.execute(
                    """
                    INSERT INTO messages(id, channel_id, author_id, peer_id, user_id, content, created_at_ms)
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (message_id, channel_id, author_id, peer_id, user_id, content, int(time_now_ms or 0))
                )
                if os.environ.get("TEST_MODE"):
                    print(f"[message projector] Successfully inserted message {message_id} to channel {channel_id}")
            else:
                if os.environ.get("TEST_MODE"):
                    print(f"[message projector] Message {message_id} already exists, skipping insert")
            # Commit is managed by the framework transaction
    except Exception as e:
        if os.environ.get("TEST_MODE"):
            print(f"[message projector] Exception persisting message: {e}")
            import traceback
            traceback.print_exc()
        pass

    # Unblock any events waiting for this message
    try:
        unblock(db, message_id)
    except Exception:
        pass

    return db