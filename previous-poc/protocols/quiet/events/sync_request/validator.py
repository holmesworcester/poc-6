"""Validator for sync request events."""

from typing import Dict, Any
from core.core_types import validator


@validator
def validate(envelope: Dict[str, Any]) -> bool:
    """
    Validate a sync request event.

    Sync requests are ephemeral and have relaxed validation since
    they're not stored permanently.

    Returns:
        True if valid, False if invalid
    """
    event_data = envelope['event_plaintext']

    # Check required fields (type already verified by generic validator)
    # Allow sealed requests that target a prekey without an explicit to_peer.
    # Minimal required fields:
    required_fields = ['request_id', 'network_id', 'created_by', 'timestamp_ms']
    for field in required_fields:
        if field not in event_data:
            return False

    # Check that IDs are not empty
    if not event_data['request_id']:
        return False

    if not event_data['network_id']:
        return False

    if not event_data['created_by']:
        return False

    # to_peer is optional when sealing to a prekey; receivers can derive it.

    # Validate timestamp is reasonable
    timestamp = event_data.get('timestamp_ms', 0)
    if not isinstance(timestamp, int) or timestamp <= 0:
        return False

    # Transit secret is optional but if present should be non-empty
    transit_secret = event_data.get('transit_secret')
    if transit_secret is not None and not transit_secret:
        return False

    # Relax membership requirement for bootstrap: sealed requests are not stored
    # and will be handled by reflector logic. Validators remain structure-only.

    return True
