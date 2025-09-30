import json
import base64


def execute(input_data, db):
    """
    Returns an invite-link containing this peer, for sharing out-of-band
    """
    # Get the identityId from path parameters
    identity_id = input_data.get('identityId')
    if not identity_id:
        return {
            "api_response": {
                "return": "Error: Identity ID not provided",
                "error": "identityId is required"
            }
        }
    
    # Lookup identity in SQL
    identity_data = None
    if hasattr(db, 'conn'):
        try:
            cur = db.conn.cursor()
            row = cur.execute(
                "SELECT pubkey, name FROM identities WHERE pubkey = ? LIMIT 1",
                (identity_id,)
            ).fetchone()
            if row:
                identity_data = {"pubkey": row[0], "name": row[1]}
        except Exception:
            identity_data = None
    if not identity_data:
        return {
            "api_response": {
                "return": "Error: Identity not found",
                "error": f"No identity found with pubkey {identity_id}"
            },
            "internal": {}
        }
    
    # Create invite data with peer info
    invite_data = {
        "peer": identity_data['pubkey'],
        "name": identity_data.get('name', 'Unknown')
    }
    
    # Encode as base64 for easy sharing
    invite_json = json.dumps(invite_data, sort_keys=True)
    invite_link = f"message-via-tor://invite/{base64.urlsafe_b64encode(invite_json.encode()).decode()}"
    
    return {
        "api_response": {
            "return": "Invite link created",
            "inviteLink": invite_link,
            "inviteData": invite_data
        }
    }
