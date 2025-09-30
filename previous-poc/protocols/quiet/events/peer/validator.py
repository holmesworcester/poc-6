"""
Validator for peer events.
"""
from typing import Dict, Any
from core.core_types import validator


@validator
def validate(envelope: Dict[str, Any]) -> bool:
    """
    Validate a peer event.

    Returns True if valid, False if invalid.
    """
    event_data = envelope['event_plaintext']

    # Check required fields (type already verified by generic validator)
    # peer_id is filled by crypto handler, network_id can be empty
    # Accept peer_secret_id (local key reference)
    required_fields = ['type', 'public_key', 'created_at', 'signature']
    for field in required_fields:
        if field not in event_data:
            return False
    if 'peer_secret_id' not in event_data:
        return False
    
    # Check that public_key is not empty
    if not event_data['public_key']:
        return False
    
    # Check that peer id reference is not empty
    if not (event_data.get('peer_secret_id')):
        return False
    
    # network_id can be empty when peer is created before network
    
    # Check that created_at is a positive integer
    if not isinstance(event_data['created_at'], int) or event_data['created_at'] <= 0:
        return False
    
    # Peer events are signed by the public key they contain
    # The signature handler will verify this
    
    return True
