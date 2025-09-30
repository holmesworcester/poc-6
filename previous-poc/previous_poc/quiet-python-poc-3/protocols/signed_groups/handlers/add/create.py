import hashlib
from core import crypto

def execute(params, db):
    """
    Adds a user to a group
    """
    user_id = params.get('user_id')  # User being added
    group_id = params.get('group_id')
    added_by = params.get('added_by')  # User doing the adding
    
    if not user_id or not group_id or not added_by:
        raise ValueError("user_id, group_id, and added_by are required")
    
    # Validate everything exists and permissions (SQL-only)
    if not hasattr(db, 'conn'):
        raise ValueError("Persistent DB required")
    cursor = db.conn.cursor()
    # Check group exists
    grow = cursor.execute("SELECT id, created_by FROM groups WHERE id = ?", (group_id,)).fetchone()
    if not grow:
        raise ValueError(f"Group {group_id} not found")
    group_created_by = grow[1] if not isinstance(grow, dict) else grow.get('created_by')
    # Check user being added exists
    urow = cursor.execute("SELECT id, network_id FROM users WHERE id = ?", (user_id,)).fetchone()
    if not urow:
        raise ValueError(f"User {user_id} not found")
    user_network = urow[1] if not isinstance(urow, dict) else urow.get('network_id')
    # Check adder exists and is in same network
    arow = cursor.execute(
        "SELECT id, pubkey FROM users WHERE id = ? AND network_id = ?",
        (added_by, user_network)
    ).fetchone()
    if not arow:
        raise ValueError(f"User {added_by} not found or not in same network")
    adder_pubkey = arow[1] if not isinstance(arow, dict) else arow.get('pubkey')
    # Check if adder has permission (must be group creator or already in group)
    if added_by != group_created_by:
        existing = cursor.execute(
            "SELECT id FROM adds WHERE user_id = ? AND group_id = ?",
            (added_by, group_id)
        ).fetchone()
        if not existing:
            raise ValueError(
                f"User {added_by} does not have permission to add users to group {group_id}"
            )
    
    # Generate add ID
    add_id = hashlib.sha256(f"{user_id}:{group_id}:{added_by}".encode()).hexdigest()[:16]
    
    # Get private key for signing
    irow = cursor.execute("SELECT privkey FROM identities WHERE pubkey = ?", (adder_pubkey,)).fetchone()
    if not irow:
        raise ValueError(f"Identity private key not found for user {added_by}")
    privkey = irow[0] if not isinstance(irow, dict) else irow.get('privkey')
    
    # Create signature (real or dummy based on crypto mode)
    sig_data = f"add:{add_id}:{group_id}:{user_id}:{added_by}"
    signature = crypto.sign(sig_data, privkey)
    
    # Create add event
    add_event = {
        'type': 'add',
        'id': add_id,
        'user_id': user_id,
        'group_id': group_id,
        'added_by': added_by,
        'signature': signature
    }
    
    return {
        'api_response': {
            'addId': add_id,
            'userId': user_id,
            'groupId': group_id
        },
        'newEvents': [add_event]
    }
