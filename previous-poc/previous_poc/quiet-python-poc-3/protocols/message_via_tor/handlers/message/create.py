from core.crypto import sign
import json
import time


def execute(input_data, db):
    """
    Create a new message event command.
    Creates canonical signed event and broadcasts it to all known peers of the identity.
    """
    # Get message text
    text = input_data.get("text")
    if not text:
        raise ValueError("Message text is required")
    
    # Get sender identity from input
    sender_id = input_data.get("senderId")
    if not sender_id:
        raise ValueError("Sender identity (senderId) is required")
    
    # Fetch identity from SQL
    private_key = None
    public_key = None
    if hasattr(db, 'conn'):
        try:
            cur = db.conn.cursor()
            row = cur.execute(
                "SELECT pubkey, privkey FROM identities WHERE pubkey = ? LIMIT 1",
                (sender_id,)
            ).fetchone()
            if row:
                public_key = row[0]
                private_key = row[1]
        except Exception:
            pass
    if not private_key or not public_key:
        raise ValueError(f"Identity not found: {sender_id}")
    
    # Get current time from input or use current time
    time_now_ms = input_data.get("time_now_ms", int(time.time() * 1000))
    
    # Create canonical event data
    event_data = {
        "type": "message",
        "text": text,
        "sender": public_key,
        "timestamp": time_now_ms
    }
    
    # Sign the canonical event
    data_str = json.dumps(event_data, sort_keys=True)
    signature = sign(data_str, private_key)
    event_data["sig"] = signature
    
    # Get all peers known by this identity (SQL-only)
    known_peers = []
    if hasattr(db, 'conn'):
        try:
            cur = db.conn.cursor()
            rows = cur.execute(
                "SELECT pubkey FROM peers WHERE received_by = ?",
                (public_key,)
            ).fetchall()
            known_peers = [dict(r) for r in rows]
        except Exception:
            known_peers = []
    # Dict-state deprecated; no fallback
    
    
    # Create outgoing envelope for each known peer
    sent_count = 0
    outgoing_list = []
    for peer in known_peers:
        peer_pubkey = peer.get('pubkey')
        if not peer_pubkey:
            continue
        
        # Don't send to self
        if public_key == peer_pubkey:
            continue
        
        # Persist to SQL outgoing if available
        try:
            if hasattr(db, 'conn'):
                cur = db.conn.cursor()
                cur.execute(
                    "INSERT INTO outgoing(recipient, data, created_at, sent) VALUES(?, ?, ?, 0)",
                    (peer_pubkey, json.dumps(event_data), int(input_data.get('time_now_ms') or 0))
                )
        except Exception:
            pass
        sent_count += 1
        outgoing_list.append({"recipient": peer_pubkey, "data": event_data})
    
    return {
        "api_response": {
            "return": f"Message broadcast to {sent_count} peers",
            "messageId": f"msg-{time_now_ms}",
            "sentTo": sent_count
        },
        "newEvents": [event_data]
    }
