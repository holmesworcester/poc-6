"""
Validator for key_secret events (local-only symmetric secrets).
"""
from typing import Any
from core.core_types import validator


@validator
def validate(envelope: dict[str, Any]) -> bool:
    event = envelope['event_plaintext']

    # Type already verified by generic validator
    unsealed = event.get('unsealed_secret')
    if not unsealed:
        return False
    # Accept hex string or bytes for local-only secret
    if not (isinstance(unsealed, (bytes, bytearray)) or isinstance(unsealed, str)):
        return False
    return True

