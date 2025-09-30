def execute(input_data, db):
    """
    Given a peer public key, returns all messages known to that peer.
    Prefer SQL when available; fallback to in-memory state otherwise.
    """
    # API passes peerId as path parameter
    peer_pubkey = input_data.get("peerId")
    if not peer_pubkey:
        return {"api_response": {"return": "Error: No peer ID provided", "error": "Missing peerId"}}

    messages_out = []

    if hasattr(db, 'conn'):
        try:
            cur = db.conn.cursor()
            rows = cur.execute(
                """
                SELECT text, sender, recipient, received_by, timestamp, event_id
                FROM messages
                WHERE received_by = ? AND (unknown_peer IS NULL OR unknown_peer = 0)
                ORDER BY timestamp
                """,
                (peer_pubkey,)
            ).fetchall()
            for r in rows:
                messages_out.append({
                    "text": r[0],
                    "sender": r[1],
                    "recipient": r[2],
                    "timestamp": r[4]
                })
        except Exception:
            messages_out = []

    # Dict-state deprecated; no fallback

    return {"api_response": {"return": f"Found {len(messages_out)} messages", "messages": messages_out}}
