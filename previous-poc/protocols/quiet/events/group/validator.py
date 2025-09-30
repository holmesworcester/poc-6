"""
Validator for group events.
"""
from typing import Dict, Any, Set
from core.core_types import validator


@validator
def validate(envelope: dict[str, Any]) -> bool:
    """
    Validate a group event.

    Returns True if valid, False otherwise.
    """
    event = envelope['event_plaintext']

    # Check required fields (type already verified by generic validator)
    required: Set[str] = {'type', 'network_id', 'created_by', 'name', 'created_at', 'signature'}
    if not required.issubset(event.keys()):
        return False
    
    # Group-specific validation
    # Check creator matches peer_id (the signer) if available
    if 'peer_id' in envelope and event['created_by'] != envelope['peer_id']:
        return False
    
    return True
