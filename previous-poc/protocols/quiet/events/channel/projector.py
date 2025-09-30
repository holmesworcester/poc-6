"""
Projector for channel events.
"""
from typing import Dict, Any, List


def project(envelope: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Project channel event to state.
    Returns deltas to apply.
    """
    event_data = envelope.get('event_plaintext', {})
    # Channel ID is the event_id for channel events
    channel_id = envelope['event_id']
    group_id = event_data['group_id']
    network_id = event_data['network_id']
    name = event_data['name']
    creator_id = event_data['creator_id']
    created_at = event_data['created_at']
    description = event_data.get('description', '')
    
    # Return deltas for creating a channel
    deltas = [
        {
            'op': 'insert',
            'table': 'channels',
            'data': {
                'channel_id': channel_id,
                'group_id': group_id,
                'network_id': network_id,
                'name': name,
                'creator_id': creator_id,
                'created_at': created_at,
                'description': description
            }
        }
    ]
    
    return deltas
