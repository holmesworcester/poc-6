"""
Projector for address events.
"""
from typing import Dict, Any, List


def project(envelope: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Project an address event into the addresses table.

    Returns a list of deltas.
    """
    event_data = envelope.get('event_plaintext', {})
    action = event_data.get('action', 'add')

    if action == 'add':
        # Add or update address
        deltas = [
            {
                'op': 'upsert',
                'table': 'addresses',
                'key': {
                    'peer_id': event_data['peer_id'],
                    'ip': event_data['ip'],
                    'port': event_data['port']
                },
                'data': {
                    'peer_id': event_data['peer_id'],
                    'ip': event_data['ip'],
                    'port': event_data['port'],
                    'network_id': event_data['network_id'],
                    'registered_at_ms': event_data['timestamp_ms'],
                    'is_active': True
                }
            }
        ]
    elif action == 'remove':
        # Mark address as inactive
        deltas = [
            {
                'op': 'update',
                'table': 'addresses',
                'key': {
                    'peer_id': event_data['peer_id'],
                    'ip': event_data['ip'],
                    'port': event_data['port']
                },
                'data': {
                    'is_active': False
                }
            }
        ]
    else:
        deltas = []

    return deltas