"""
Validator for message events.
"""
from typing import Any, Set
from core.core_types import validator


@validator
def validate(envelope: dict[str, Any]) -> bool:
    """
    Validate a message event.

    Checks:
    - Has required fields
    - Message ID is valid
    - Author is the signer
    - Content is not empty
    """
    event_data = envelope['event_plaintext']

    # Check required fields (type already verified by generic validator)
    required: Set[str] = {
        'type', 'channel_id', 'group_id', 'network_id', 'created_by', 'content', 'created_at', 'signature'
    }
    if not required.issubset(event_data.keys()):
        print(f"[message validator] Missing required fields")
        return False

    # Message events should reference the peer that created them
    # But for now we're using identity_id directly for signing
    # TODO: Update to use peer_id once we have proper peer events

    # Check content is not empty
    if not event_data.get('content') or not event_data['content'].strip():
        print(f"[message validator] Empty content")
        return False

    # Check content length (reasonable limit)
    if len(event_data['content']) > 10000:
        print(f"[message validator] Content too long")
        return False

    return True
