import hashlib
import json
import base64
from core import crypto

def execute(params, db):
    """
    Creates an invite link for the network
    """
    identity_id = params.get('identityId')
    
    if not identity_id:
        raise ValueError("identityId is required")
    
    # Use SQL to find identity, user, network, and first group
    if not hasattr(db, 'conn'):
        raise ValueError("Persistent DB required")
    cur = db.conn.cursor()
    # Identity
    row = cur.execute("SELECT name FROM identities WHERE pubkey = ?", (identity_id,)).fetchone()
    identity_name = row[0] if row else None
    if not identity_name:
        raise ValueError(f"Identity {identity_id} not found")
    # User for identity
    urow = cur.execute("SELECT id, network_id FROM users WHERE pubkey = ?", (identity_id,)).fetchone()
    
    if not urow:
        raise ValueError(f"User for identity {identity_id} not found")
    user_id = urow[0] if not isinstance(urow, dict) else urow.get('id')
    network_id = urow[1] if not isinstance(urow, dict) else urow.get('network_id')
    # Network
    nrow = cur.execute("SELECT id, name FROM networks WHERE id = ?", (network_id,)).fetchone()
    if not nrow:
        raise ValueError("No network found")
    network = {'id': nrow[0] if not isinstance(nrow, dict) else nrow.get('id'), 'name': nrow[1] if not isinstance(nrow, dict) else nrow.get('name')}
    # Determine first group id from networks table when available; otherwise derive and store
    nrow2 = cur.execute("SELECT first_group_id FROM networks WHERE id = ?", (network['id'],)).fetchone()
    first_group_id = None
    if nrow2 and (nrow2[0] if not isinstance(nrow2, dict) else nrow2.get('first_group_id')):
        first_group_id = nrow2[0] if not isinstance(nrow2, dict) else nrow2.get('first_group_id')
    if not first_group_id:
        # Fallback: pick any existing group (earliest if created_at_ms available)
        grow = cur.execute("SELECT id FROM groups ORDER BY created_at_ms LIMIT 1").fetchone()
        if not grow:
            raise ValueError("No groups found - at least one group is required")
        first_group_id = grow[0] if not isinstance(grow, dict) else grow.get('id')
        # Persist first_group_id for this network for future enforcement (transaction managed by framework)
        try:
            cur.execute("UPDATE networks SET first_group_id = ? WHERE id = ?", (first_group_id, network['id']))
        except Exception:
            pass
    
    # Generate invite secret and derive public key
    import time
    import os
    if os.environ.get("TEST_MODE") == "1":
        # Deterministic generation for tests
        secret_data = f"{user_id}:{network_id}:{first_group_id}"
    else:
        time_ms = int(time.time() * 1000)
        secret_data = f"{user_id}:{time_ms}"
    invite_secret = "invite_secret_" + hashlib.sha256(secret_data.encode()).hexdigest()[:16]
    # Use KDF to derive the same pubkey that join.py will derive
    # Use deterministic salt for invites so both parties derive same key
    invite_salt = hashlib.sha256(b"signed_groups_invite_kdf_v1").digest()[:16]
    kdf_result = crypto.kdf(invite_secret, salt=invite_salt)
    invite_pubkey = "invite_pub_" + kdf_result['derived_key'][:32]
    invite_id = hashlib.sha256(invite_pubkey.encode()).hexdigest()[:16]
    
    # Get the user's private key to sign the invite
    urow2 = cur.execute("SELECT pubkey FROM users WHERE id = ?", (user_id,)).fetchone()
    if not urow2:
        raise ValueError(f"User {user_id} not found")
    user_pubkey = urow2[0] if not isinstance(urow2, dict) else urow2.get('pubkey')
    
    # Get the private key for this pubkey
    irow = cur.execute("SELECT privkey FROM identities WHERE pubkey = ?", (user_pubkey,)).fetchone()
    if not irow:
        raise ValueError(f"Identity private key not found for user {user_id}")
    privkey = irow[0] if not isinstance(irow, dict) else irow.get('privkey')
    
    # Create signature (real or dummy based on crypto mode)
    signature_data = f"invite:{invite_id}:{invite_pubkey}:{network_id}:{first_group_id}:{user_id}"
    signature = crypto.sign(signature_data, privkey)
    
    # Create invite event
    invite_event = {
        'type': 'invite',
        'id': invite_id,
        'invite_pubkey': invite_pubkey,
        'network_id': network['id'],
        'group_id': first_group_id,
        'created_by': user_id,
        'signature': signature
    }
    
    # Create invite link data (include group_id in link)
    invite_data = {
        'invite_secret': invite_secret,
        'network_id': network['id'],
        'network_name': network['name'],
        'group_id': first_group_id
    }
    
    # Encode invite link
    invite_json = json.dumps(invite_data)
    invite_b64 = base64.b64encode(invite_json.encode()).decode()
    invite_link = f"signed-groups://invite/{invite_b64}"
    
    return {
        'api_response': {
            'inviteLink': invite_link,
            'inviteId': invite_id
        },
        'newEvents': [invite_event]
    }
