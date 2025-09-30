"""Reflector for key events to emit key_secret (local-only) when a key is unsealed."""

from typing import Dict, Any, List, Tuple


def key_reflector(envelope: Dict[str, Any], db, time_now_ms: int) -> Tuple[bool, List[Dict[str, Any]]]:
    try:
        # Only emit if this key event has local unsealed material
        unsealed = envelope.get('unsealed_secret')
        if not unsealed:
            return True, []

        event = envelope.get('event_plaintext', {})
        # No deterministic functional ID: local-only key_secret now uses standard
        # event_id derived from ciphertext under the root_secret. Keep created_at.
        created_at = event.get('created_at') or time_now_ms

        # Emit local-only key_secret event (no group_id)
        from core.crypto import get_root_secret_id
        root_id = get_root_secret_id()

        # Normalize unsealed secret to hex string
        if isinstance(unsealed, (bytes, bytearray)):
            unsealed_hex = bytes(unsealed).hex()
        else:
            unsealed_hex = str(unsealed)

        secret_env = {
            'event_type': 'key_secret',
            'event_plaintext': {
                'type': 'key_secret',
                'unsealed_secret': unsealed_hex,
                'created_at': created_at,
            },
            'local_only': True,
            'self_created': True,
            'deps': [root_id],
            'key_secret_id': root_id,
        }
        return True, [secret_env]
    except Exception:
        return False, []
