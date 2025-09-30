import hashlib
from core import crypto

def execute(params, db):
    """
    Creates a new channel within a network
    """
    name = params.get('name')
    network_id = params.get('network_id')
    user_id = params.get('user_id')
    group_id = params.get('group_id')
    
    if not name or not network_id or not user_id or not group_id:
        raise ValueError("Channel name, network_id, user_id, and group_id are required")
    
    # Validate user exists and belongs to network (SQL-only)
    if not hasattr(db, 'conn'):
        raise ValueError("Persistent DB required")
    cursor = db.conn.cursor()
    row = cursor.execute(
        "SELECT id, pubkey, network_id FROM users WHERE id = ? AND network_id = ?",
        (user_id, network_id)
    ).fetchone()
    if not row:
        raise ValueError(f"User {user_id} not found in network {network_id}")
    # Validate group exists
    grow = cursor.execute("SELECT id FROM groups WHERE id = ?", (group_id,)).fetchone()
    if not grow:
        raise ValueError(f"Group {group_id} not found")
    
    # Generate channel ID
    channel_data = f"{name}:{network_id}:{user_id}:{group_id}"
    channel_id = hashlib.sha256(channel_data.encode()).hexdigest()[:16]
    
    # Get user's pubkey and then private key for signing
    user_pubkey = row[1] if not isinstance(row, dict) else row.get('pubkey')
    
    # Get the private key
    irow = cursor.execute("SELECT privkey FROM identities WHERE pubkey = ?", (user_pubkey,)).fetchone()
    if not irow:
        raise ValueError(f"Identity private key not found for user {user_id}")
    privkey = irow[0] if not isinstance(irow, dict) else irow.get('privkey')
    
    # Create signature (real or dummy based on crypto mode)
    sig_data = f"channel:{channel_id}:{name}:{network_id}:{user_id}"
    if group_id:
        sig_data += f":{group_id}"
    signature = crypto.sign(sig_data, privkey)
    
    # Create channel event
    channel_event = {
        'type': 'channel',
        'id': channel_id,
        'name': name,
        'network_id': network_id,
        'group_id': group_id,
        'created_by': user_id,  # Use created_by to match projector expectations
        'signature': signature
    }
    
    return {
        'api_response': {
            'channelId': channel_id,
            'name': name,
            'networkId': network_id,
            'groupId': group_id
        },
        'newEvents': [channel_event]
    }
