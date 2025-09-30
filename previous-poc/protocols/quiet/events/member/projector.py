"""
Projector for member events.
"""
from typing import Dict, Any, List


def project(envelope: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Project a member event into the group_members table.
    
    Returns a list of deltas.
    """
    event_data = envelope.get('event_plaintext', {})
    # Return delta for adding a group member
    deltas = [
        {
            'op': 'insert',
            'table': 'group_members',
            'data': {
                'group_id': event_data['group_id'],
                'user_id': event_data['user_id'],
                'added_by': event_data['added_by'],
                'added_at': event_data['created_at']
            }
        }
    ]
    
    return deltas