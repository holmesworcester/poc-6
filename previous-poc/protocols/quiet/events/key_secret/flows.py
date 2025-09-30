"""
Flow for creating a local-only key_secret for event-layer encryption.
"""
from __future__ import annotations

from typing import Dict, Any
import time
import hashlib

from core.crypto import generate_secret, get_root_secret_id
from core.flows import FlowCtx, flow_op


@flow_op()  # Registers as 'key_secret.create'
def create(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create a local-only key_secret and return its id.

    Optional params:
    - secret_hex: provide a specific 32-byte hex secret; otherwise a random one is generated.
    """
    ctx = FlowCtx.from_params(params)

    secret_hex = params.get('secret_hex')
    if isinstance(secret_hex, str) and len(secret_hex) >= 64:
        try:
            secret_bytes = bytes.fromhex(secret_hex)
        except Exception:
            secret_bytes = generate_secret()
    else:
        secret_bytes = generate_secret()

    root_id = get_root_secret_id()

    key_secret_actual_id = ctx.emit_event({
        'event_type': 'key_secret',
        'event_plaintext': {
            'type': 'key_secret',
            'unsealed_secret': secret_bytes.hex(),
            'created_at': int(time.time() * 1000),
        },
        'local_only': True,
        'deps': [root_id],
        'self_created': True,
        'key_secret_id': root_id,
    })
    return {
        'ids': {'key_secret': key_secret_actual_id},
        'data': {'key_secret_id': key_secret_actual_id}
    }
