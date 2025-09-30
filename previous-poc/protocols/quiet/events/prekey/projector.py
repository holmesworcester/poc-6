"""
Projector for prekey events.
"""
from typing import Dict, Any, List


def project(envelope: Dict[str, Any]) -> List[Dict[str, Any]]:
    event = envelope.get('event_plaintext', {})
    prekey_id = event['prekey_id']
    peer_id = event['peer_id']
    network_id = event['network_id']
    public_key = event['public_key']
    created_at = event['created_at']
    expires_at = event.get('expires_at')

    if isinstance(public_key, str):
        try:
            public_key = bytes.fromhex(public_key)
        except Exception:
            public_key = b''

    return [
        {
            'op': 'insert',
            'table': 'prekeys',
            'data': {
                'prekey_id': prekey_id,
                'peer_id': peer_id,
                'network_id': network_id,
                'public_key': public_key,
                'created_at': created_at,
                'expires_at': expires_at,
                'active': 1,
            }
        }
    ]

