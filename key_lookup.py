"""Unified key lookup for unwrapping (checks transit keys, group keys, and prekeys)."""
from typing import Any
import logging
import crypto
import store
from db import create_unsafe_db, create_safe_db

log = logging.getLogger(__name__)

ID_SIZE = 16  # bytes (128 bits)


def extract_id(blob: bytes) -> bytes:
    """Extract the first ID_SIZE bytes from a wrapped blob."""
    return blob[:ID_SIZE]


def get_key_by_id(id_bytes: bytes, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get key from database by id bytes. Checks transit keys, group keys, and prekeys.

    Args:
        id_bytes: Key ID bytes (16 bytes) - hint ID from blob
        recorded_by: Peer ID attempting to access this key (for ownership filtering)
        db: Database connection

    Returns:
        Key dict for crypto.unwrap(), or None if not found or not accessible
    """
    key_id = crypto.b64encode(id_bytes)

    log.debug(f"get_key_by_id() looking up key_id={key_id}, recorded_by={recorded_by[:20]}...")

    # First try transit keys table (device-wide, check ownership)
    unsafedb = create_unsafe_db(db)
    transit_row = unsafedb.query_one(
        "SELECT key, owner_peer_id FROM transit_keys WHERE key_id = ?",
        (key_id,)
    )
    if transit_row and transit_row['owner_peer_id'] == recorded_by:
        log.debug(f"get_key_by_id() found transit key for key_id={key_id}")
        return {
            'id': id_bytes,
            'key': transit_row['key'],
            'type': 'symmetric'
        }

    # Then try group keys table (subjective)
    safedb = create_safe_db(db, recorded_by=recorded_by)
    group_row = safedb.query_one(
        "SELECT key FROM group_keys WHERE key_id = ? AND recorded_by = ?",
        (key_id, recorded_by)
    )
    if group_row:
        log.debug(f"get_key_by_id() found group key for key_id={key_id}")
        return {
            'id': id_bytes,
            'key': group_row['key'],
            'type': 'symmetric'
        }

    log.debug(f"get_key_by_id() key_id={key_id} not found in keys tables")

    # Then try asymmetric keys from transit_prekeys (device-wide, with ownership filter)
    # The hint (key_id) is always a prekey_shared_id (event ID)
    # Two cases:
    # 1. Detached prekey (from invite_accepted): prekey_id = prekey_shared_id directly
    # 2. Regular prekey: need to find transit_prekey_id from transit_prekeys_shared, then look up in transit_prekeys

    log.debug(f"get_key_by_id() checking transit_prekeys for prekey_id={key_id} (detached), owner={recorded_by[:20]}...")
    transit_prekey_row = unsafedb.query_one(
        "SELECT private_key FROM transit_prekeys WHERE prekey_id = ? AND owner_peer_id = ? LIMIT 1",
        (key_id, recorded_by)
    )
    if transit_prekey_row and transit_prekey_row['private_key']:
        log.debug(f"get_key_by_id() found transit prekey private key for prekey_id={key_id}")
        return {
            'id': id_bytes,
            'private_key': transit_prekey_row['private_key'],
            'type': 'asymmetric'
        }

    # Try finding via transit_prekeys_shared (for regular prekeys)
    log.debug(f"get_key_by_id() checking transit_prekeys_shared for prekey_shared_id={key_id}")
    transit_prekey_shared_row = safedb.query_one(
        "SELECT transit_prekey_shared_id FROM transit_prekeys_shared WHERE transit_prekey_shared_id = ? AND recorded_by = ? LIMIT 1",
        (key_id, recorded_by)
    )
    if transit_prekey_shared_row:
        # Need to get transit_prekey_id from event data
        transit_prekey_shared_blob = store.get(key_id, db)
        if transit_prekey_shared_blob:
            import crypto as crypto_module
            transit_prekey_shared_data = crypto_module.parse_json(transit_prekey_shared_blob)
            transit_prekey_id = transit_prekey_shared_data.get('transit_prekey_id')
            if transit_prekey_id:
                transit_prekey_row = unsafedb.query_one(
                    "SELECT private_key FROM transit_prekeys WHERE prekey_id = ? AND owner_peer_id = ? LIMIT 1",
                    (transit_prekey_id, recorded_by)
                )
                if transit_prekey_row and transit_prekey_row['private_key']:
                    log.debug(f"get_key_by_id() found transit prekey via transit_prekeys_shared link")
                    return {
                        'id': id_bytes,
                        'private_key': transit_prekey_row['private_key'],
                        'type': 'asymmetric'
                    }

    # Then try group_prekeys (subjective) - need to look up via group_prekeys_shared
    log.debug(f"get_key_by_id() checking group_prekeys_shared for group_prekey_shared_id={key_id}")
    group_prekey_shared_row = safedb.query_one(
        "SELECT group_prekey_shared_id FROM group_prekeys_shared WHERE group_prekey_shared_id = ? AND recorded_by = ? LIMIT 1",
        (key_id, recorded_by)
    )
    if group_prekey_shared_row:
        # Need to get group_prekey_id from event data
        group_prekey_shared_blob = store.get(key_id, db)
        if group_prekey_shared_blob:
            import crypto as crypto_module
            group_prekey_shared_data = crypto_module.parse_json(group_prekey_shared_blob)
            group_prekey_id = group_prekey_shared_data.get('group_prekey_id')
            if group_prekey_id:
                group_prekey_row = safedb.query_one(
                    "SELECT private_key FROM group_prekeys WHERE prekey_id = ? AND recorded_by = ? LIMIT 1",
                    (group_prekey_id, recorded_by)
                )
                if group_prekey_row and group_prekey_row['private_key']:
                    log.debug(f"get_key_by_id() found group prekey via group_prekeys_shared link")
                    return {
                        'id': id_bytes,
                        'private_key': group_prekey_row['private_key'],
                        'type': 'asymmetric'
                    }

    # Finally, try main peer private key from local_peers (with ownership filter)
    log.debug(f"get_key_by_id() checking local_peers for peer_id={key_id}")
    peer_row = unsafedb.query_one(
        "SELECT private_key FROM local_peers WHERE peer_id = ?",
        (key_id,)
    )
    if peer_row and peer_row['private_key'] and key_id == recorded_by:
        log.debug(f"get_key_by_id() found peer private key for peer_id={key_id}")
        return {
            'id': id_bytes,
            'private_key': peer_row['private_key'],
            'type': 'asymmetric'
        }

    log.debug(f"get_key_by_id() NO KEY FOUND for key_id={key_id}, recorded_by={recorded_by[:20]}...")
    return None
