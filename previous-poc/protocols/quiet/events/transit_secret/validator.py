"""
Validator for local-only transit_secret events.
"""
from core.core_types import validator
from typing import Any


@validator
def validate(envelope: dict[str, Any]) -> bool:
    event = envelope.get('event_plaintext') or {}
    # Require unsealed_secret (hex or bytes)
    sec = event.get('unsealed_secret') or event.get('transit_secret')
    if not isinstance(sec, (str, bytes, bytearray)):
        return False
    # Optional metadata to help jobs and attribution
    # network_id, dest_ip, dest_port, peer_secret_id, peer_id may be present
    return True
