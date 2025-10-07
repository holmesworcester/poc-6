"""Network and peer management functions."""
from typing import Any
from db import create_unsafe_db

def get_peer_id_for_transit_key(hint: bytes, db: Any) -> str:
    """Get the peer ID associated with a transit key hint"""
    unsafedb = create_unsafe_db(db)
    result = unsafedb.query_one("SELECT peer_id FROM transit_keys WHERE hint = ?", (hint,))
    return result['peer_id'] if result else ""

# TODO: add create() and list() functions for network events
