import hashlib
import os
import time
from core import crypto

def execute(params, db):
    """
    Creates a new message in a channel
    """
    channel_id = params.get('channel_id')
    user_id = params.get('user_id')
    peer_id = params.get('peer_id')
    content = params.get('content')
    
    if not channel_id or not user_id or not peer_id or not content:
        raise ValueError("channel_id, user_id, peer_id, and content are required")
    
    # Validate channel exists and permissions via SQL-only
    if not hasattr(db, 'conn'):
        raise ValueError("Persistent DB required")
    cursor = db.conn.cursor()
    crow = cursor.execute("SELECT id, network_id FROM channels WHERE id = ?", (channel_id,)).fetchone()
    if not crow:
        raise ValueError(f"Channel {channel_id} not found")
    channel_network = crow[1] if not isinstance(crow, dict) else crow.get('network_id')
    urow = cursor.execute(
        "SELECT id, pubkey, network_id FROM users WHERE id = ? AND network_id = ?",
        (user_id, channel_network)
    ).fetchone()
    if not urow:
        raise ValueError(f"User {user_id} not found in channel's network")
    # For messages, peer_id should be the user_id (for self-messages) or a linked device
    if peer_id != user_id:
        link_row = cursor.execute(
            "SELECT id FROM links WHERE peer_id = ? AND user_id = ?",
            (peer_id, user_id)
        ).fetchone()
        if not link_row:
            raise ValueError(f"Peer {peer_id} is not authorized for user {user_id}")
    
    # Generate message ID
    if os.environ.get("TEST_MODE") == "1":
        # Deterministic ID for tests
        msg_data = f"{channel_id}:{user_id}:{peer_id}:{content}"
    else:
        time_ms = int(time.time() * 1000)
        msg_data = f"{channel_id}:{user_id}:{time_ms}:{content[:32]}"
    message_id = hashlib.sha256(msg_data.encode()).hexdigest()[:16]
    
    # Get the peer's private key for signing (peer signs messages)
    # First get peer's pubkey
    if peer_id == user_id:
        # Self-message: user signs with their own key
        peer_pubkey = urow[1] if not isinstance(urow, dict) else urow.get('pubkey')
    else:
        # Linked device: verify link exists
        prow = cursor.execute("SELECT id FROM links WHERE peer_id = ? AND user_id = ?", (peer_id, user_id)).fetchone()
        if not prow:
            raise ValueError(f"No link found between peer {peer_id} and user {user_id}")
        # For linked devices, the peer_id IS the pubkey
        peer_pubkey = peer_id
    
    # Get private key
    irow = cursor.execute("SELECT privkey FROM identities WHERE pubkey = ?", (peer_pubkey,)).fetchone()
    if not irow:
        raise ValueError(f"Identity private key not found for peer {peer_id}")
    privkey = irow[0] if not isinstance(irow, dict) else irow.get('privkey')
    
    # Create signature (real or dummy based on crypto mode)
    sig_data = f"message:{message_id}:{channel_id}:{user_id}:{peer_id}:{content}"
    signature = crypto.sign(sig_data, privkey)
    
    # Create message event
    message_event = {
        'type': 'message',
        'id': message_id,
        'channel_id': channel_id,
        'author_id': user_id,
        'peer_id': peer_id,
        'user_id': user_id,
        'content': content,
        'text': content,  # For compatibility
        'signature': signature
    }
    
    return {
        'api_response': {
            'messageId': message_id,
            'channelId': channel_id
        },
        'newEvents': [message_event]
    }
