"""
Projector for remove_user events.

Projects removal to removed_users table and cascades to remove_peer.
NOTE: Does NOT delete the user event itself - user events remain in database
for historical record (late joiners need to know who sent messages).
"""
from typing import Dict, Any, List


def project(envelope: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Project remove_user event to state.

    Creates records:
    1. removed_users table: marks user as removed
    2. removed_peers table: cascades removal to all peers of this user
       (since all peers of a removed user can no longer sync)

    This tracks removal for authorization checks, but does NOT delete
    historical user data. Late joiners can still see user events and messages.

    Returns deltas to apply.
    """
    event_data = envelope.get('event_plaintext', {})
    event_id = envelope.get('event_id')

    user_id = event_data.get('user_id')
    network_id = event_data.get('network_id')
    removed_at = event_data.get('removed_at')

    # Extract signer (who removed this user)
    # The signature handler verified this matches the peer_pk in common header
    peer_pk = event_data.get('peer_pk', envelope.get('peer_pk', ''))

    deltas = [
        {
            'op': 'insert',
            'table': 'removed_users',
            'data': {
                'user_id': user_id,
                'removed_at': removed_at,
                'removed_by': peer_pk,
                'reason': 'removal_event'
            }
        }
    ]

    # Cascade: Also remove all peers belonging to this user
    # Query to find all peers for this user and remove them
    deltas.append({
        'op': 'cascade_remove_peers',
        'user_id': user_id,
        'removed_at': removed_at,
        'removed_by': peer_pk
    })

    return deltas
