"""
Projector for peer events.
"""
from typing import Dict, Any, List
from core.core_types import projector


@projector
def project(envelope: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Project a peer event into the peers table.

    Returns a list of deltas.
    """
    event_data = envelope.get('event_plaintext', {})

    # Return delta for creating a peer record
    # peer_id will be set by handler as hash of event
    deltas = [
        {
            'op': 'insert',
            'table': 'peers',
            'data': {
                'peer_id': envelope.get('event_id', ''),  # Hash of event
                'public_key': event_data.get('public_key', ''),
                'peer_secret_id': (event_data.get('peer_secret_id') or ''),
                'created_at': event_data.get('created_at', 0)
            }
        }
    ]

    return deltas
