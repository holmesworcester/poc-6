"""Pre-key management functions."""
from typing import Any


def get_transit_prekey_for_peer(peer_id: str, db: Any) -> Any:
    """Get the transit pre-key for a specific peer."""
    # Query the pre-keys table by peer_id and return the pre-key
    pre_key = db.query("SELECT pre_key FROM pre_keys WHERE peer_id = ?", (peer_id,)) # Todo: make this pick a short-lived prekey
    return pre_key
