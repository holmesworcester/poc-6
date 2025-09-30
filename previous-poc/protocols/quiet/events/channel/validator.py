"""
Validator for channel events.
"""
from typing import Dict, Any, Set
from core.core_types import validator


@validator
def validate(envelope: dict[str, Any]) -> bool:
    """
    Validate a channel event.

    Returns True if valid, False otherwise.
    """
    event = envelope['event_plaintext']

    # Check required fields (type already verified by generic validator)
    # Note: channel uses 'creator_id' not 'created_by' for consistency with database schema
    required: Set[str] = {'type', 'group_id', 'network_id', 'creator_id', 'name', 'created_at'}
    if not required.issubset(event.keys()):
        return False

    # Signature is required for non-self-created events (added by signature handler)
    if not envelope.get('self_created') and 'signature' not in event:
        return False

    # Channel-specific validation
    # Check creator matches peer_id (the signer) if available
    if 'peer_id' in envelope and event['creator_id'] != envelope['peer_id']:
        return False
    
    # TODO: In a full implementation, we'd check if the group exists
    # and if the creator is a member of the group
    
    return True
