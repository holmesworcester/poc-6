"""
Validator for prekey events.
"""
from typing import Dict, Any
from core.core_types import validator


@validator
def validate(envelope: Dict[str, Any]) -> bool:
    """Validate a prekey publication event."""
    event = envelope['event_plaintext']

    # Check required fields (type already verified by generic validator)
    required = {'type', 'prekey_id', 'peer_id', 'network_id', 'public_key', 'created_at', 'signature'}
    if not required.issubset(event.keys()):
        return False

    # Basic shape checks
    pk = event.get('public_key')
    if isinstance(pk, str):
        try:
            bytes.fromhex(pk)
        except Exception:
            return False
    elif not isinstance(pk, (bytes, bytearray)):
        return False

    # prekey_id should be hex-ish and non-empty
    prekey_id = event.get('prekey_id')
    if not isinstance(prekey_id, str) or not prekey_id:
        return False

    return True

