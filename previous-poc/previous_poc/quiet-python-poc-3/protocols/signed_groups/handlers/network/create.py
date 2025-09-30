import hashlib
from core import crypto

def execute(params, db):
    """
    Creates a new network and adds creator as first user
    """
    name = params.get('name')
    identity_id = params.get('identityId')
    
    if not name or not identity_id:
        raise ValueError("Network name and identityId are required")
    
    # Find identity via SQL (dict-state deprecated)
    identity_name = None
    privkey = None
    if hasattr(db, 'conn'):
        try:
            cur = db.conn.cursor()
            row = cur.execute("SELECT name, privkey FROM identities WHERE pubkey = ?", (identity_id,)).fetchone()
            if row:
                identity_name = row[0] if not isinstance(row, dict) else row.get('name')
                privkey = row[1] if not isinstance(row, dict) else row.get('privkey')
        except Exception:
            pass
    if identity_name is None or privkey is None:
        raise ValueError(f"Identity {identity_id} not found")
    
    # Generate network ID (hash of name + creator)
    network_id_data = f"{name}:{identity_id}".encode()
    network_id = hashlib.sha256(network_id_data).hexdigest()[:16]
    
    # Create network event (no signature needed - bootstrap event)
    network_event = {
        'type': 'network',
        'id': network_id,
        'name': name,
        'creator_pubkey': identity_id
    }
    
    # Create user ID for creator
    user_id = hashlib.sha256(f"{network_id}:{identity_id}".encode()).hexdigest()[:16]
    
    # Create default group ID first
    group_id = hashlib.sha256(f"{network_id}:default_group".encode()).hexdigest()[:16]
    
    # Now create user event with group_id
    user_signature_data = f"user:{user_id}:{network_id}:{group_id}:{identity_id}:{identity_name}"
    user_signature = crypto.sign(user_signature_data, privkey)
    
    user_event = {
        'type': 'user',
        'id': user_id,
        'network_id': network_id,
        'pubkey': identity_id,
        'name': identity_name,
        'group_id': group_id,  # Add group_id so user is associated with General group
        'signature': user_signature
    }
    
    # Create default group event
    group_signature_data = f"group:{group_id}:General:{user_id}"
    group_signature = crypto.sign(group_signature_data, privkey)
    
    group_event = {
        'type': 'group',
        'id': group_id,
        'name': 'General',
        'created_by': user_id,
        'signature': group_signature
    }
    
    # Add creator to the group
    add_id = hashlib.sha256(f"{user_id}:{group_id}".encode()).hexdigest()[:16]
    add_signature_data = f"add:{add_id}:{user_id}:{group_id}:{user_id}"
    add_signature = crypto.sign(add_signature_data, privkey)
    
    add_event = {
        'type': 'add',
        'id': add_id,
        'user_id': user_id,
        'group_id': group_id,
        'added_by': user_id,
        'signature': add_signature
    }
    
    # Create default channel in the General group
    channel_id = hashlib.sha256(f"{network_id}:{group_id}:general-chat".encode()).hexdigest()[:16]
    channel_signature_data = f"channel:{channel_id}:general:{network_id}:{user_id}:{group_id}"
    channel_signature = crypto.sign(channel_signature_data, privkey)
    
    channel_event = {
        'type': 'channel',
        'id': channel_id,
        'network_id': network_id,
        'group_id': group_id,
        'name': 'general',
        'created_by': user_id,
        'signature': channel_signature
    }
    
    return {
        'api_response': {
            'networkId': network_id,
            'name': name,
            'userId': user_id,
            'firstGroupId': group_id,
            'defaultChannelId': channel_id
        },
        'newEvents': [network_event, user_event, group_event, add_event, channel_event]
    }
