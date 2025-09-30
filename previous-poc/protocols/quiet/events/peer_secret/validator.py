"""
Validator for peer_secret events (local-only identity material).
"""
from core.core_types import validator
from typing import Any


@validator
def validate(envelope: dict[str, Any]) -> bool:
    """Validate a peer_secret event (local-only)."""
    event = envelope['event_plaintext']

    # Accept both new and legacy id fields for compatibility
    has_id = ('peer_secret_id' in event)
    if not has_id:
        return False

    required_common = ['type', 'name', 'public_key', 'private_key', 'created_at']
    for k in required_common:
        if k not in event:
            return False
    return True
