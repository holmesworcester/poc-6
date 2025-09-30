import json
import base64
import hashlib
from core import crypto

def execute(params, db):
    """
    Join network using invite link
    """
    identity_id = params.get('identityId')
    invite_link = params.get('inviteLink')
    
    if not identity_id or not invite_link:
        raise ValueError("identityId and inviteLink are required")
    
    # Parse invite link
    if not invite_link.startswith("signed-groups://invite/"):
        raise ValueError("Invalid invite link format")
    
    invite_b64 = invite_link[23:]  # Remove prefix
    try:
        invite_json = base64.b64decode(invite_b64).decode()
        invite_data = json.loads(invite_json)
    except:
        raise ValueError("Invalid invite link encoding")
    
    invite_secret = invite_data.get('invite_secret')
    network_id = invite_data.get('network_id')
    group_id = invite_data.get('group_id')
    
    if not invite_secret or not network_id or not group_id:
        raise ValueError(f"Invalid invite data - missing required fields. Got: {invite_data}")
    
    # Find identity via SQL
    if not hasattr(db, 'conn'):
        raise ValueError("Persistent DB required")
    cur = db.conn.cursor()
    row = cur.execute("SELECT name FROM identities WHERE pubkey = ?", (identity_id,)).fetchone()
    if not row:
        raise ValueError(f"Identity {identity_id} not found")
    identity_name = row[0] if not isinstance(row, dict) else row.get('name')
    
    # Derive invite pubkey from secret using KDF
    # Use same deterministic salt as create.py
    invite_salt = hashlib.sha256(b"signed_groups_invite_kdf_v1").digest()[:16]
    kdf_result = crypto.kdf(invite_secret, salt=invite_salt)
    invite_pubkey = "invite_pub_" + kdf_result['derived_key'][:32]
    
    # Find matching invite via SQL
    inv_row = cur.execute("SELECT id, network_id, group_id FROM invites WHERE invite_pubkey = ?", (invite_pubkey,)).fetchone()
    if not inv_row:
        # Add debugging info
        all_invites = list(cur.execute("SELECT id, invite_pubkey, network_id FROM invites"))
        error_msg = f"Invite not found or invalid. Looking for pubkey: {invite_pubkey}. "
        error_msg += f"Found {len(all_invites)} invites in database: "
        for inv in all_invites:
            error_msg += f"[id={inv[0]}, pubkey={inv[1][:20]}..., network={inv[2]}] "
        raise ValueError(error_msg)
    invite = {
        'id': inv_row[0] if not isinstance(inv_row, dict) else inv_row.get('id'),
        'network_id': inv_row[1] if not isinstance(inv_row, dict) else inv_row.get('network_id'),
        'group_id': inv_row[2] if not isinstance(inv_row, dict) else inv_row.get('group_id')
    }
    
    # Generate user ID
    user_id = hashlib.sha256(f"{network_id}:{identity_id}:{invite['id']}".encode()).hexdigest()[:16]
    
    # Get the private key for the identity to sign with
    identity_row = cur.execute("SELECT privkey FROM identities WHERE pubkey = ?", (identity_id,)).fetchone()
    if not identity_row:
        raise ValueError(f"Identity private key not found for {identity_id}")
    privkey = identity_row[0] if not isinstance(identity_row, dict) else identity_row.get('privkey')
    
    # Create signatures (real or dummy based on crypto mode)
    # Sign the user event data
    sig_data = f"user:{user_id}:{network_id}:{group_id}:{identity_id}:{identity_name}"
    signature = crypto.sign(sig_data, privkey)
    
    # Create invite signature
    # In dummy mode, this will create a dummy signature
    # In real mode, we use a hash-based signature since invites don't have real keys
    inv_sig_data = f"{invite_secret}:{user_id}:{network_id}"
    invite_signature = crypto.hash(inv_sig_data)[:64]  # Use hash as invite "signature"
    
    # Create user event
    user_event = {
        'type': 'user',
        'id': user_id,
        'network_id': network_id,
        'group_id': group_id,  # From invite
        'pubkey': identity_id,
        'name': identity_name,
        'invite_id': invite['id'],
        'invite_signature': invite_signature,
        'signature': signature
    }
    
    # Get accessible channels for the joined user
    # Users can access channels in groups they belong to
    accessible_channels = []
    
    # Debug: Check channels table
    import os
    if os.environ.get('VERBOSE'):
        all_channels = cur.execute("SELECT id, name, network_id, group_id FROM channels").fetchall()
        print(f"[DEBUG] All channels in DB: {len(all_channels)}")
        for ch in all_channels:
            print(f"[DEBUG]   Channel: id={ch[0]}, name={ch[1]}, net={ch[2]}, group={ch[3]}")
        print(f"[DEBUG] Looking for channels with group_id={group_id}, network_id={network_id}")
    
    # Get channels in the joined group
    channel_rows = cur.execute("""
        SELECT id, name, group_id 
        FROM channels 
        WHERE group_id = ? AND network_id = ?
    """, (group_id, network_id)).fetchall()
    
    for row in channel_rows:
        accessible_channels.append({
            'id': row[0],
            'name': row[1],
            'group_id': row[2]
        })
    
    # Find the general channel
    default_channel_id = None
    for ch in accessible_channels:
        if ch['name'] == 'general':
            default_channel_id = ch['id']
            break
    
    return {
        'api_response': {
            'userId': user_id,
            'networkId': network_id,
            'accessibleChannels': accessible_channels,
            'defaultChannelId': default_channel_id
        },
        'newEvents': [user_event]
    }
