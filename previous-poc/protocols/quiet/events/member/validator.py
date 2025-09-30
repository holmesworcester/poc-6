"""
Validator for member events.
"""
from typing import Dict, Any
from core.core_types import validator


@validator
def validate(envelope: Dict[str, Any]) -> bool:
    """
    Validate a member event.

    Returns True if valid, False if invalid.
    """
    event_data = envelope['event_plaintext']

    # Check required fields (type already verified by generic validator)
    required_fields = ['type', 'group_id', 'user_id', 'added_by', 'network_id', 'created_at', 'signature']
    for field in required_fields:
        if field not in event_data:
            return False

    # Check added_by matches peer_id (the signer)
    if event_data['added_by'] != envelope.get('peer_id'):
        return False

    # Check that user_id and group_id are not empty
    if not event_data['user_id'] or not event_data['group_id']:
        return False

    # TODO: In a full implementation, we'd check:
    # - If the group exists
    # - If the added_by user has permission to add members
    # - If the user_id is valid

    return True