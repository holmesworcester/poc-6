"""
Validator for key events.
"""
from typing import Dict, Any, Set
from core.core_types import validator


@validator
def validate(envelope: dict[str, Any]) -> bool:
    """
    Validate a key event.

    Returns True if valid, False otherwise.
    """
    event = envelope['event_plaintext']

    # Check required fields (type already verified by generic validator)
    required: Set[str] = {'type', 'peer_id', 'network_id', 'created_at', 'signature', 'sealed_key'}
    if not required.issubset(event.keys()):
        return False
    
    # Key-specific validation
    # Additional validation for key_id length
    if 'key_id' in event and len(event.get('key_id', '')) != 64:  # 32 bytes hex
        return False
        
    if 'sealed_key' in event and not event['sealed_key']:
        return False
        
    return True
