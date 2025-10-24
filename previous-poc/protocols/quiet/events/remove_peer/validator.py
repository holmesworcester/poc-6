"""
Validator for remove_peer events.

Authorization rules (from ideal_protocol_design.md §Removal):
- Any peer can remove themselves and their linked peers
- Admins can remove any peer
"""
from typing import Any
from core.core_types import validator


@validator
def validate(envelope: dict[str, Any]) -> bool:
    """
    Validate a remove_peer event.

    Checks:
    1. Required fields present
    2. Signer (in envelope) has authority to remove the target peer
    3. Authority: self-removal, linked peer removal, or admin removal

    Returns True if valid, False otherwise.
    """
    event = envelope.get('event_plaintext', {})

    # Check required fields
    required_fields = ['type', 'peer_id', 'network_id', 'removed_at']
    for field in required_fields:
        if field not in event:
            return False

    # peer_id (target being removed) must not be empty
    if not event.get('peer_id'):
        return False

    # network_id must not be empty
    if not event.get('network_id'):
        return False

    # removed_at must be a valid timestamp
    removed_at = event.get('removed_at')
    if not isinstance(removed_at, (int, float)) or removed_at <= 0:
        return False

    # The signer is who issued the removal
    # This is verified by the signature handler, we trust it here

    # Note: Authorization checks (self-removal, linked peers, admin status)
    # require database access and are deferred to the blocker handler.
    # Events with insufficient permission will be blocked there.

    return True
