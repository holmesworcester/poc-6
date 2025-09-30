"""
Projector for group events.
"""
from typing import Dict, Any, List
import json


def project(envelope: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Project group event to state.
    Returns deltas to apply.
    """
    event_data = envelope.get('event_plaintext', {})
    # Group ID is the event_id for group events
    group_id = envelope['event_id']
    network_id = event_data['network_id']
    name = event_data['name']
    creator_id = event_data['created_by']
    created_at = event_data['created_at']
    permissions = event_data.get('permissions', {})
    
    # Return deltas for creating a group
    deltas = [
        {
            'op': 'insert',
            'table': 'groups',
            'data': {
                'group_id': group_id,
                'network_id': network_id,
                'name': name,
                'creator_id': creator_id,
                'owner_id': creator_id,  # Initially, creator is the owner
                'created_at': created_at,
                'permissions': json.dumps(permissions)
            }
        },
        # Creator is automatically a member
        {
            'op': 'insert',
            'table': 'group_members',
            'data': {
                'group_id': group_id,
                'user_id': creator_id,
                'added_by': creator_id,
                'added_at': created_at
            }
        }
    ]
    
    return deltas
