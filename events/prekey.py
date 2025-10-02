"""Pre-key management functions."""
from typing import Any
import crypto


def get_transit_prekey_for_peer(peer_id: str, db: Any) -> dict[str, Any] | None:
    """Get the transit pre-key for a specific peer in format expected by crypto.wrap().

    Args:
        peer_id: Peer ID to get prekey for
        db: Database connection

    Returns:
        Key dict with format {'id': bytes, 'public_key': bytes, 'type': 'asymmetric'}
        or None if prekey not found
    """
    result = db.query_one("SELECT public_key FROM pre_keys WHERE peer_id = ?", (peer_id,))
    if not result:
        return None

    # Use peer_id as the hint/id for asymmetric keys
    peer_id_bytes = crypto.b64decode(peer_id)

    return {
        'id': peer_id_bytes,
        'public_key': result['public_key'],
        'type': 'asymmetric'
    }
