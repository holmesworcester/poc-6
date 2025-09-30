"""
Validator for invite events.
"""
from typing import Dict, Any


def validate(envelope: Dict[str, Any]) -> bool:
    """
    Validate an invite event.

    Returns True if valid, False if invalid.
    """
    event_data = envelope['event_plaintext']

    # Check required fields (type already verified by generic validator)
    required_fields = ['invite_pubkey', 'network_id', 'group_id', 'inviter_id', 'created_at']
    for field in required_fields:
        if field not in event_data:
            return False

    # Check that invite_pubkey is not empty
    if not event_data['invite_pubkey']:
        return False

    # Check inviter matches peer_id (the signer)
    if event_data['inviter_id'] != envelope.get('peer_id'):
        return False

    return True