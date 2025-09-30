def execute(input_data, db):
    """
    Sends sync requests from all identities to all their known peers (SQL-only).
    """
    cur = db.conn.cursor()
    identities = [dict(r) for r in cur.execute("SELECT pubkey FROM identities").fetchall()]
    peers = [dict(r) for r in cur.execute("SELECT pubkey, received_by FROM peers").fetchall()]
    
    sent_count = 0
    
    # From each identity, send sync request to peers known by that identity
    for identity_obj in identities:
        identity_pubkey = identity_obj.get('pubkey')
        if not identity_pubkey:
            continue
        
        # Find peers known by this specific identity
        known_peers = [p for p in peers if p.get('received_by') == identity_pubkey]
            
        for peer in known_peers:
            peer_pubkey = peer.get('pubkey')
            if not peer_pubkey:
                continue
            
            # Don't send to self
            if identity_pubkey == peer_pubkey:
                continue
            
            # Create sync_peers event
            sync_event = {
                "type": "sync_peers",
                "sender": identity_pubkey
            }
            
            # Create outgoing envelope
            outgoing = {
                "recipient": peer_pubkey,
                "data": sync_event
            }
            
            # Persist to SQL outgoing if available
            try:
                if hasattr(db, 'conn'):
                    import json
                    cur = db.conn.cursor()
                    cur.execute(
                        "INSERT INTO outgoing(recipient, data, created_at, sent) VALUES(?, ?, ?, 0)",
                        (peer_pubkey, json.dumps(sync_event), int(input_data.get('time_now_ms') or 0))
                    )
            except Exception:
                pass
            sent_count += 1
    
    # Count unique recipients
    rows = cur.execute("SELECT DISTINCT recipient FROM outgoing WHERE sent = 0").fetchall()
    unique_recipients = len(rows)
    
    return {"api_response": {"return": f"Sent sync requests from {len(identities)} identities to {unique_recipients} peers", "identities": len(identities), "uniqueRecipients": unique_recipients}}
