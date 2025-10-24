"""
Validator for remove_user events.

Authorization rules (from ideal_protocol_design.md §Removal):
- Peers linked to a user can remove that user
- Admins can remove any user, including other admins
"""
from typing import Any
from core.core_types import validator


@validator
def validate(envelope: dict[str, Any]) -> bool:
    """
    Validate a remove_user event.

    Checks:
    1. Required fields present
    2. Signer (in envelope) has authority to remove the target user
    3. Authority: user removing self (via linked peer) or admin removal

    Returns True if valid, False otherwise.
    """
    event = envelope.get('event_plaintext', {})

    # Check required fields
    required_fields = ['type', 'user_id', 'network_id', 'removed_at']
    for field in required_fields:
        if field not in event:
            return False

    # user_id (target being removed) must not be empty
    if not event.get('user_id'):
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

    # Note: Authorization checks (user removing self, admin status)
    # require database access and are deferred to the blocker handler.
    # Events with insufficient permission will be blocked there.

    return True
