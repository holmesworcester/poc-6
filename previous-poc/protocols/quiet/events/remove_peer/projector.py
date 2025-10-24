"""
Projector for remove_peer events.

Projects removal to removed_peers table.
NOTE: Does NOT delete the peer event itself - peer events remain in database
for historical record (late joiners need to see who removed whom).
"""
from typing import Dict, Any, List


def project(envelope: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Project remove_peer event to state.

    Creates a record in removed_peers table tracking:
    - Which peer was removed
    - When they were removed
    - Who removed them (the signer)

    This tracks removal for authorization checks, but does NOT delete
    historical peer data. Late joiners can still see peer events.

    Returns deltas to apply.
    """
    event_data = envelope.get('event_plaintext', {})
    event_id = envelope.get('event_id')

    peer_id = event_data.get('peer_id')
    network_id = event_data.get('network_id')
    removed_at = event_data.get('removed_at')

    # Extract signer (who removed this peer)
    # The signature handler verified this matches the peer_pk in common header
    peer_pk = event_data.get('peer_pk', envelope.get('peer_pk', ''))

    deltas = [
        {
            'op': 'insert',
            'table': 'removed_peers',
            'data': {
                'peer_id': peer_id,
                'removed_at': removed_at,
                'removed_by': peer_pk,
                'reason': 'removal_event'
            }
        }
    ]

    return deltas
