"""Crypto functions for wrapping and unwrapping."""
from typing import Any

def unwrap_and_store(blob, t_ms, db):
    hint=key.parse_hint(blob)
    seen_by = network.get_peer_id_for_transit_key_hint(hint, db)
    unwrapped_blob = unwrap(blob, hint, db)
    seen_id = store.with_seen(unwrapped_blob, seen_by, t_ms, db)
    return seen_id

def unwrap(wrapped_blob, hint, db):
    """look up key hint, determine if sym or asym, fetch key"""
    """unseal or decrypt a blob with the key"""
    return unwrapped_blob # another blob or plaintext

def wrap(plaintext: dict[str, Any], key: Any, db: Any) -> bytes:
    """Look up key, determine if sym or asym, fetch key"""
    """Wrap plaintext with a key and return the encrypted blob."""
    # TODO: implement
    return wrapped_blob  # some bytes