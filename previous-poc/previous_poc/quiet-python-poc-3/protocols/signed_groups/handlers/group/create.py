import hashlib
from core import crypto

def execute(params, db):
    """
    Creates a new group within a network
    """
    name = params.get('name')
    user_id = params.get('user_id')
    
    if not name or not user_id:
        raise ValueError("Group name and user_id are required")
    
    # Validate user via SQL only
    if not hasattr(db, 'conn'):
        raise ValueError("Persistent DB required")
    cursor = db.conn.cursor()
    row = cursor.execute("SELECT id, pubkey FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise ValueError(f"User {user_id} not found")
    user_pubkey = row[1] if not isinstance(row, dict) else row.get('pubkey')
    
    # Generate group ID
    group_id = hashlib.sha256(f"{name}:{user_id}".encode()).hexdigest()[:16]
    
    # Get private key for signing
    irow = cursor.execute("SELECT privkey FROM identities WHERE pubkey = ?", (user_pubkey,)).fetchone()
    if not irow:
        raise ValueError(f"Identity private key not found for user {user_id}")
    privkey = irow[0] if not isinstance(irow, dict) else irow.get('privkey')
    
    # Create signature (real or dummy based on crypto mode)
    sig_data = f"group:{group_id}:{name}:{user_id}"
    signature = crypto.sign(sig_data, privkey)
    
    # Create group event
    group_event = {
        'type': 'group',
        'id': group_id,
        'name': name,
        'created_by': user_id,
        'signature': signature
    }
    
    # Also add the creator to the group
    add_id = hashlib.sha256(f"{user_id}:{group_id}:creator".encode()).hexdigest()[:16]
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
    
    return {
        'api_response': {
            'groupId': group_id,
            'name': name
        },
        'newEvents': [group_event, add_event]
    }
