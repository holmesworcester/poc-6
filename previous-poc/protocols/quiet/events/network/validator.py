"""
Validator for network events.
"""
from typing import Dict, Any, Set
from core.core_types import validator


@validator
def validate(envelope: dict[str, Any]) -> bool:
    """
    Validate a network event.

    Returns True if valid, False if invalid.
    """
    event = envelope['event_plaintext']

    # Check required fields (type already verified by generic validator)
    required: Set[str] = {'type', 'name', 'created_by', 'created_at', 'signature'}
    if not required.issubset(event.keys()):
        return False
    
    # Check that name is not empty
    if not event.get('name', '').strip():
        return False
    
    # Check creator matches identity_id (the signer) for self-created
    # or peer_id for received events
    signer_id = envelope.get('peer_id')
    if event.get('created_by') != signer_id:
        return False
    
    # For now, don't check duplicates - that would require db access
    # Network events are valid if they have all required fields
    return True
