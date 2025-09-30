"""
Flow to create a local-only peer_secret (keypair).
"""
from __future__ import annotations

from typing import Dict, Any
import time
import hashlib

from core.crypto import generate_keypair, get_root_secret_id
from core.flows import FlowCtx, flow_op


@flow_op()  # Registers as 'peer_secret.create'
def create(params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = FlowCtx.from_params(params)
    name = (params.get('name') or 'User')

    priv, pub = generate_keypair()

    root_id = get_root_secret_id()

    peer_secret_actual_id = ctx.emit_event({
        'event_type': 'peer_secret',
        'event_plaintext': {
            'type': 'peer_secret',
            'name': name,
            'public_key': pub.hex(),
            'private_key': priv.hex(),
            'created_at': int(time.time() * 1000),
        },
        'local_only': True,
        'deps': [root_id],
        'self_created': True,
        'key_secret_id': root_id,
    })
    # Optionally create the canonical peer event immediately when a key is provided
    peer_id = None
    key_secret_id = params.get('key_secret_id')
    if isinstance(key_secret_id, str) and key_secret_id:
        peer_env = {
            'event_type': 'peer',
            'event_plaintext': {
                'type': 'peer',
                'public_key': pub.hex(),
                'peer_secret_id': peer_secret_actual_id,
                'created_at': int(time.time() * 1000),
                'created_by': peer_secret_actual_id,
            },
            'peer_id': peer_secret_actual_id,
            'deps': [key_secret_id, peer_secret_actual_id],
            'self_created': True,
            'key_secret_id': key_secret_id,
        }
        peer_id = ctx.emit_event(peer_env)

    return {
        'ids': {
            'peer_secret': peer_secret_actual_id,
            **({'peer': peer_id} if isinstance(peer_id, str) and peer_id else {}),
        },
        'data': {
            'peer_secret_id': peer_secret_actual_id,
            'public_key': pub.hex(),
            **({'peer_id': peer_id} if isinstance(peer_id, str) and peer_id else {}),
        }
    }
