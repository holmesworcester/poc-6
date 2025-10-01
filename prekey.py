"""Pre-key management functions."""
from typing import Any


def get_transit_prekey_for_peer(peer_id: str, db: Any) -> Any:
    """Get the transit pre-key for a specific peer."""
    # TODO: make this pick a short-lived prekey
    result = db.query_one("SELECT pre_key FROM pre_keys WHERE peer_id = ?", (peer_id,))
    return result['pre_key'] if result else None
