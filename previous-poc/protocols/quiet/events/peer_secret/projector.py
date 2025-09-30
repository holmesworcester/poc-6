"""
Projector for peer_secret events (local-only identity storage).

Projects into the local `peer_secrets` table which stores the
private key material for the local peer (never gossiped).
"""
from typing import Dict, Any, List
from core.core_types import projector


@projector
def project(envelope: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Project a peer_secret event into the peer_secrets table."""
    event = envelope.get('event_plaintext') or {}

    # Always use the real pipeline-assigned event_id as the peer_secret_id
    peer_secret_id = envelope.get('event_id') or ''
    name = event.get('name', 'User')
    public_key = event.get('public_key', '')
    private_key = event.get('private_key', b'')
    if isinstance(private_key, str):
        try:
            private_key = bytes.fromhex(private_key)
        except Exception:
            private_key = b''
    created_at = event.get('created_at', 0)

    deltas: List[Dict[str, Any]] = [
        {
            'op': 'insert',
            'table': 'peer_secrets',
            'data': {
                'peer_secret_id': peer_secret_id,
                'name': name,
                'public_key': public_key,
                'private_key': private_key,
                'created_at': created_at,
            },
        }
    ]
    return deltas
