def project(db, envelope, time_now_ms):
    """
    Validates sync request and sends peer events known by the receiving identity to requester
    """
    # Get data and metadata from envelope
    data = envelope.get('payload', {})
    metadata = envelope.get('metadata', {})
    
    if data.get('type') != 'sync_peers':
        return db
    
    sender = data.get('sender')
    if not sender:
        return db
    
    # Get which identity received this sync request
    received_by = metadata.get('received_by')
    if not received_by:
        # Can't determine which identity received this, skip
        return db
    
    # Get peers known by the identity that received this sync request
    peers = []
    known_peers = []
    if hasattr(db, 'conn'):
        try:
            cur = db.conn.cursor()
            rows = cur.execute(
                "SELECT pubkey, name FROM peers WHERE received_by = ?",
                (received_by,)
            ).fetchall()
            known_peers = [{'pubkey': r[0], 'name': r[1]} for r in rows]
        except Exception:
            known_peers = []
    # Dict-state deprecated: do not fall back to in-memory peers

    # Send peer events for peers known by this identity
    if known_peers:
        for peer in known_peers:
            # Create peer event data to send
            peer_event = {
                'type': 'peer',
                'pubkey': peer.get('pubkey'),
                'name': peer.get('name')
            }
            # Persist to SQL outgoing
            try:
                if hasattr(db, 'conn'):
                    import json
                    cur = db.conn.cursor()
                    cur.execute(
                        "INSERT INTO outgoing(recipient, data, created_at, sent) VALUES(?, ?, ?, 0)",
                        (sender, json.dumps(peer_event), int(time_now_ms or 0))
                    )
                    # Commit is managed by the framework transaction
            except Exception:
                pass
    
    # Append sync_peers to SQL event_store (protocol-owned)
    try:
        if hasattr(db, 'conn'):
            import json as _json
            cur = db.conn.cursor()
            evt_type = 'sync_peers'
            evt_id = metadata.get('eventId') or None
            pubkey = received_by or sender or 'unknown'
            cur.execute(
                """
                INSERT OR IGNORE INTO event_store(pubkey, event_data, metadata, event_type, event_id, created_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    pubkey,
                    _json.dumps(data, sort_keys=True),
                    _json.dumps(metadata, sort_keys=True),
                    evt_type,
                    evt_id,
                    int(time_now_ms or 0),
                )
            )
            # Commit is managed by the framework transaction
    except Exception:
        pass
    
    return db
