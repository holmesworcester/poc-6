"""
Validator for prekey_secret events (local-only KEM material).
"""
from core.core_types import validator


@validator
def validate(envelope: dict[str, any]) -> bool:
    event = envelope['event_plaintext']
    # Require fields used by unseal
    return (
        isinstance(event.get('prekey_private'), (bytes, bytearray, str)) and
        isinstance(event.get('prekey_public'), (bytes, bytearray, str)) and
        isinstance(event.get('created_at'), int)
    )

