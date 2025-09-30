"""
Validator for address events.
"""
from typing import Dict, Any
from core.core_types import validator


@validator
def validate(envelope: Dict[str, Any]) -> bool:
    """
    Validate an address event.

    Returns True if valid, False if invalid.
    """
    event_data = envelope['event_plaintext']

    # Check required fields (type already verified by generic validator)
    # Note: signature is added by the signature handler, not required at validation time
    required_fields = ['type', 'action', 'peer_id', 'ip', 'port', 'network_id', 'timestamp_ms']
    for field in required_fields:
        if field not in event_data:
            return False

    # Check action is valid
    if event_data['action'] not in ['add', 'remove']:
        return False

    # Check that peer_id is not empty
    if not event_data['peer_id']:
        return False

    # Check that network_id is not empty
    if not event_data['network_id']:
        return False

    # Validate port is in valid range
    port = event_data.get('port')
    if not isinstance(port, int) or port < 1 or port > 65535:
        return False

    # Validate IP is not empty
    if not event_data['ip'] or not isinstance(event_data['ip'], str):
        return False

    # Check peer_id matches envelope peer_id (the signer)
    if event_data['peer_id'] != envelope.get('peer_id'):
        return False

    return True
