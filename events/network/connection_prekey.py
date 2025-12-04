"""Connection prekey event type (device-wide prekey keypair for initial contact).

This is a renamed wrapper around transit_prekey for naming consistency.
All connection-related concepts use the `connection_` prefix.

Usage:
    from events.network import connection_prekey

    # Get prekey for a peer to send initial connection request
    to_key = connection_prekey.get_prekey_for_peer(peer_shared_id, local_peer_id, db)
"""

# Re-export everything from transit_prekey with consistent naming
from events.network.transit_prekey import (
    # Constants
    TRANSIT_PREKEY_TTL_MS as CONNECTION_PREKEY_TTL_MS,
    MIN_TRANSIT_PREKEYS as MIN_CONNECTION_PREKEYS,
    REPLENISH_TRANSIT_PREKEYS as REPLENISH_CONNECTION_PREKEYS,

    # Functions
    generate_batch,
    create,
    create_with_material,
    project,
    replenish_for_all_peers,
)

# Registry metadata (same as transit_prekey)
EVENT_TYPE = 'connection_prekey'  # Alias - actual events still use 'transit_prekey'
SHAREABLE = False
EPHEMERAL = False
PROJECTION_TABLE = None


def get_prekey_for_peer(peer_shared_id: str, recorded_by: str, db) -> dict | None:
    """Get the connection prekey for a specific peer.

    Renamed from get_transit_prekey_for_peer for consistency.

    Args:
        peer_shared_id: Peer's peer_shared_id (public identity) to get prekey for
        recorded_by: Local peer_id requesting access (for subjective view)
        db: Database connection

    Returns:
        Key dict with format {'id': bytes, 'public_key': bytes, 'type': 'asymmetric'}
        or None if prekey not found
    """
    from events.network.transit_prekey import get_transit_prekey_for_peer
    return get_transit_prekey_for_peer(peer_shared_id, recorded_by, db)
