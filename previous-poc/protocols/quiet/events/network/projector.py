"""
Projector for network events.
"""
from typing import Dict, Any, List


def project(envelope: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Project a network event into the networks table.
    
    Returns a list of deltas.
    """
    event_data = envelope.get('event_plaintext', {})
    # Network ID is the event_id for network events
    network_id = envelope['event_id']

    # Return delta for creating a network
    deltas = [
        {
            'op': 'insert',
            'table': 'networks',
            'data': {
                'network_id': network_id,
                'name': event_data['name'],
                'creator_id': event_data['created_by'],
                'created_at': event_data['created_at']
            }
        }
    ]
    
    return deltas
