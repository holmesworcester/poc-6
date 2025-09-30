"""
Projector for user events.
"""
from typing import Dict, Any, List


def project(envelope: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Project user event to state.

    User events represent a peer joining the network via invite
    and establish their membership in a group.

    Returns deltas to apply.
    """
    event_data = envelope.get('event_plaintext', {})
    # User ID is the event_id for user events
    user_id = envelope['event_id']
    peer_id = event_data['peer_id']
    network_id = event_data['network_id']
    group_id = event_data['group_id']
    name = event_data['name']
    # Invite may be absent for non-invite user creation
    invite_pubkey = event_data.get('invite_pubkey', '')
    created_at = event_data['created_at']

    # Return deltas for creating user record and group membership
    deltas = [
        {
            'op': 'insert',
            'table': 'users',
            'data': {
                'user_id': user_id,
                'peer_id': peer_id,
                'network_id': network_id,
                'name': name,
                'joined_at': created_at,
                'invite_pubkey': invite_pubkey
            }
        },
        {
            'op': 'insert',
            'table': 'group_members',
            'data': {
                'group_id': group_id,
                'user_id': user_id,
                'added_by': event_data.get('added_by', user_id),  # Who added them (self if not specified)
                'added_at': created_at
            }
        }
    ]

    return deltas
