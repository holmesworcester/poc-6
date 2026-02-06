"""TreeKEM key shared event type (shareable keys encrypted to tree nodes).

Shareable event that distributes group keys encrypted to tree node pubkeys.
This enables O(log n) key distribution by encrypting to copath nodes.
"""

# Registry metadata
EVENT_TYPE = 'treekem_key_shared'
SHAREABLE = True  # Keys sync across group
PROJECTION_TABLE = ('treekem_keys_shared', 'treekem_key_shared_id')

# Wire format constants
WIRE_TYPE_CODE = 0x42  # TYPE_TREEKEM_KEY_SHARED
WIRE_PLAINTEXT_SIZE = 312  # KEY_SHARED_PLAINTEXT_SIZE (matches group_key_shared)

from typing import Any
import logging
from core import crypto
from core import store
from core import wire_format
from core import treekem
from events.group import group_key, treekem_pubkey
from events.identity import peer
from core.db import create_safe_db
from core.projection.types import ProjectorResult, WriteOp, EmitEvent

log = logging.getLogger(__name__)


# Wire format functions

def encode_plaintext(
    key_id: bytes,
    symmetric_key: bytes,
    group_id: bytes,
    depth: int,
    path_prefix: bytes,
) -> bytes:
    """Encode a treekem_key_shared payload plaintext (pre-encryption).

    Layout (312 bytes):
    - key_id (16)
    - symmetric_key (32) - encrypted
    - group_id (16)
    - depth (2)
    - path_prefix_len (2)
    - path_prefix (up to 16 bytes)
    - pad
    """
    wire_format._require_len("key_id", key_id, 16)
    wire_format._require_len("symmetric_key", symmetric_key, wire_format.SECRET_SIZE)
    wire_format._require_len("group_id", group_id, 16)

    if len(path_prefix) > 16:
        raise ValueError("path_prefix must be at most 16 bytes")

    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:16] = key_id
    payload[16:48] = symmetric_key
    payload[48:64] = group_id
    payload[64:66] = depth.to_bytes(2, 'little')
    payload[66:68] = len(path_prefix).to_bytes(2, 'little')
    payload[68:68 + len(path_prefix)] = path_prefix
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a treekem_key_shared payload plaintext (post-decryption)."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(f"treekem_key_shared plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}")

    key_id = data[0:16]
    symmetric_key = data[16:48]
    group_id = data[48:64]
    depth = int.from_bytes(data[64:66], 'little')
    prefix_len = int.from_bytes(data[66:68], 'little')
    path_prefix = data[68:68 + prefix_len]

    return {
        "key_id": key_id,
        "symmetric_key": symmetric_key,
        "group_id": group_id,
        "depth": depth,
        "path_prefix": path_prefix,
    }


def _encrypt_payload(plaintext: bytes, key_data: dict[str, Any]) -> bytes:
    """Encrypt treekem_key_shared plaintext into wire payload."""
    if len(plaintext) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(f"treekem_key_shared plaintext must be {WIRE_PLAINTEXT_SIZE} bytes")
    key_id = wire_format._require_len("key_id", key_data.get("id", b""), 16)
    if key_data.get("type") != "asymmetric":
        raise ValueError("treekem_key_shared payload requires asymmetric key")
    sealed = crypto.seal(plaintext, key_data["public_key"])
    if len(sealed) != wire_format.PAYLOAD_SIZE - 16:
        raise ValueError("unexpected sealed length for treekem_key_shared payload")
    payload = key_id + sealed
    return wire_format._require_len("payload", payload, wire_format.PAYLOAD_SIZE)


def _decrypt_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    """Decrypt wire payload to treekem_key_shared plaintext."""
    if key_data.get("type") != "asymmetric":
        raise ValueError("treekem_key_shared payload requires asymmetric key")
    sealed = payload[16:]
    return crypto.unseal(sealed, key_data["private_key"])


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a treekem_key_shared wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(
    *,
    key_id_b64: str,
    symmetric_key_b64: str,
    group_id_b64: str,
    depth: int,
    path_prefix: bytes,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    recipient_pubkey: dict[str, Any],
    private_key: bytes,
) -> bytes:
    """Encode a complete treekem_key_shared wire event."""
    key_id = crypto.b64decode(key_id_b64)
    symmetric_key = crypto.b64decode(symmetric_key_b64)
    group_id = crypto.b64decode(group_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)

    plaintext = encode_plaintext(
        key_id=key_id,
        symmetric_key=symmetric_key,
        group_id=group_id,
        depth=depth,
        path_prefix=path_prefix,
    )
    header = wire_format.WireHeader(
        version=1,
        event_type=WIRE_TYPE_CODE,
        flags=wire_format.FLAG_ENCRYPTED | wire_format.FLAG_WRAP_ASYM,
        signer_type=wire_format.signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=wire_format._require_len("signer_id", signer_id, wire_format.SIGNER_ID_SIZE),
    )
    signed_bytes = wire_format._signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _encrypt_payload(plaintext, recipient_pubkey)
    return wire_format.build_envelope(header, payload, signature)


def decode_wire_event(
    data: bytes,
    recorded_by: str,
    db: Any,
    key_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Decode a treekem_key_shared wire event.

    Returns:
        Tuple of (event_data, missing_key_ids)
        If missing_key_ids is non-empty, decryption failed due to missing keys
    """
    header, payload, signature = wire_format.parse_envelope(data)
    if header.event_type != WIRE_TYPE_CODE:
        return None, []

    if header.flags & wire_format.FLAG_ENCRYPTED:
        # Extract the key hint (first 16 bytes of payload)
        key_hint = payload[:16]
        key_hint_b64 = crypto.b64encode(key_hint)

        # Try to find the treekem pubkey by hint
        key_data = _get_treekem_key_by_hint(key_hint, recorded_by, db)
        if not key_data:
            return None, [key_hint_b64]

        try:
            plaintext = _decrypt_payload(payload, key_data)
        except Exception as e:
            log.warning(f"Failed to decrypt treekem_key_shared: {e}")
            return None, [key_hint_b64]
    else:
        plaintext = payload[:WIRE_PLAINTEXT_SIZE]

    decoded = decode_plaintext(plaintext)
    event_data = {
        "type": EVENT_TYPE,
        "key_id": crypto.b64encode(decoded["key_id"]),
        "symmetric_key": crypto.b64encode(decoded["symmetric_key"]),
        "group_id": crypto.b64encode(decoded["group_id"]),
        "depth": decoded["depth"],
        "path_prefix": crypto.b64encode(decoded["path_prefix"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": wire_format.signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": wire_format._signing_bytes(header, plaintext),
    }
    return event_data, []


def _get_treekem_key_by_hint(key_hint: bytes, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get treekem private key by hint (treekem_pubkey_shared_id).

    The hint is the treekem_pubkey_shared_id that was used to encrypt.
    We need to find the corresponding local private key by:
    1. Looking up the shared pubkey to get (group_id, depth, path_prefix)
    2. Verifying we own this pubkey (owner_peer_shared_id matches our peer_shared_id)
    3. Looking up our local keypair by (group_id, depth, path_prefix)
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    key_hint_b64 = crypto.b64encode(key_hint)

    # Look up the shared pubkey to get tree position
    shared_row = safedb.query_one(
        """SELECT group_id, depth, path_prefix, owner_peer_shared_id
           FROM treekem_pubkeys_shared
           WHERE treekem_pubkey_shared_id = ? AND recorded_by = ?""",
        (key_hint_b64, recorded_by)
    )

    if not shared_row:
        log.debug(f"_get_treekem_key_by_hint() no shared pubkey for hint={key_hint_b64[:20]}...")
        return None

    # Check if we own this pubkey (owner matches our peer_shared_id)
    peer_self_row = safedb.query_one(
        "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
        (recorded_by, recorded_by)
    )
    if not peer_self_row:
        log.debug(f"_get_treekem_key_by_hint() no peer_self for recorded_by={recorded_by[:20]}...")
        return None

    if peer_self_row['peer_shared_id'] != shared_row['owner_peer_shared_id']:
        log.debug(f"_get_treekem_key_by_hint() pubkey not owned by us")
        return None

    # Look up our local keypair by (group_id, depth, path_prefix)
    local_row = safedb.query_one(
        """SELECT private_key FROM treekem_pubkeys
           WHERE group_id = ? AND depth = ? AND path_prefix = ? AND recorded_by = ?""",
        (shared_row['group_id'], shared_row['depth'],
         shared_row['path_prefix'], recorded_by)
    )

    if not local_row or not local_row['private_key']:
        log.debug(f"_get_treekem_key_by_hint() no local keypair for position")
        return None

    log.debug(f"_get_treekem_key_by_hint() found private key for hint={key_hint_b64[:20]}...")
    return {
        'id': key_hint,
        'private_key': local_row['private_key'],
        'type': 'asymmetric',
    }


# v2 event specification
EVENT_SPEC = {
    'encrypted': True,
    'signer': {
        'id_field': 'signed_by',
        'type_field': 'signer_type',
    },
    'requires': {},
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for treekem_key_shared events.

    On success, emits deterministic group_key event with the shared key material.
    """
    event_data = ctx.event_data

    if event_data.get('type') != EVENT_TYPE:
        return ProjectorResult(writes=tuple(), valid_event=False)

    key_id = event_data.get('key_id')
    symmetric_key_b64 = event_data.get('symmetric_key')
    group_id = event_data.get('group_id')
    depth = event_data.get('depth')
    path_prefix_b64 = event_data.get('path_prefix')
    signed_by = event_data.get('signed_by')
    created_at = event_data.get('created_at')

    if not key_id or not symmetric_key_b64 or not group_id or depth is None or not signed_by:
        return ProjectorResult(writes=tuple(), valid_event=False)

    path_prefix = crypto.b64decode(path_prefix_b64) if path_prefix_b64 else b''

    # Validate symmetric key
    try:
        crypto.b64decode(symmetric_key_b64)
    except Exception:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Create deterministic group_key event data (same as group_key_shared)
    group_key_event_data = {
        'type': 'group_key',
        'key': symmetric_key_b64,
    }

    # Verify the computed key_id matches the claimed key_id
    deterministic_blob = group_key.encode_wire_event(
        key=crypto.b64decode(symmetric_key_b64),
        created_at_ms=0,
    )
    computed_key_id = crypto.b64encode(crypto.hash(deterministic_blob))
    if computed_key_id != key_id:
        log.error(f"treekem_key_shared key_id mismatch: computed={computed_key_id[:20]}... claimed={key_id[:20]}...")
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Emit group_key event
    emit_group_key = EmitEvent(
        event_type='group_key',
        event_data=group_key_event_data,
        peer_id=None,
    )

    writes = [
        WriteOp(
            op='insert',
            table='treekem_keys_shared',
            values={
                'treekem_key_shared_id': ctx.event_id,
                'group_id': group_id,
                'key_id': computed_key_id,
                'depth': depth,
                'path_prefix': path_prefix,
                'signed_by': signed_by,
                'created_at': created_at,
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
        # Update the recipient's groups table to use this key
        # This enables key rotation: when a new key is shared via TreeKEM,
        # the recipient's view of the group's current key is updated
        WriteOp(
            op='update',
            table='groups',
            values={'key_id': computed_key_id},
            where={'group_id': group_id},
        ),
    ]

    return ProjectorResult(
        writes=tuple(writes),
        valid_event=True,
        emit_events=(emit_group_key,),
    )


def create(
    key_id: str,
    group_id: str,
    depth: int,
    path_prefix: bytes,
    recipient_pubkey_id: str,
    recipient_public_key: bytes,
    peer_id: str,
    peer_shared_id: str,
    t_ms: int,
    db: Any,
) -> str:
    """Create a treekem_key_shared event to distribute a key via tree.

    Args:
        key_id: The group key to share
        group_id: The group
        depth: Target node depth
        path_prefix: Target node path prefix
        recipient_pubkey_id: The treekem_pubkey_id to encrypt to
        recipient_public_key: The public key to encrypt to
        peer_id: Local peer ID
        peer_shared_id: Public peer ID
        t_ms: Timestamp
        db: Database connection

    Returns:
        treekem_key_shared_id
    """
    log.info(f"treekem_key_shared.create() key={key_id[:20]}..., depth={depth}")

    # Get symmetric key from local key event
    key_blob = store.get(key_id, db)
    if not key_blob:
        raise ValueError(f"key not found: {key_id}")

    if not group_key.is_wire_envelope(key_blob):
        raise ValueError("treekem_key_shared requires wire group_key event")
    key_data = group_key.decode_wire_event(key_blob)
    symmetric_key_b64 = key_data['key']

    # Build recipient pubkey dict
    recipient_pubkey = {
        'id': crypto.b64decode(recipient_pubkey_id),
        'public_key': recipient_public_key,
        'type': 'asymmetric',
    }

    # Get signing key
    private_key = peer.get_private_key(peer_id, peer_id, db)

    blob = encode_wire_event(
        key_id_b64=key_id,
        symmetric_key_b64=symmetric_key_b64,
        group_id_b64=group_id,
        depth=depth,
        path_prefix=path_prefix,
        signed_by_b64=peer_shared_id,
        signer_type='peer_shared',
        created_at_ms=t_ms,
        recipient_pubkey=recipient_pubkey,
        private_key=private_key,
    )

    key_shared_id = store.event(blob, peer_id, t_ms, db)
    log.info(f"treekem_key_shared.create() created key_shared_id={key_shared_id[:20]}...")

    return key_shared_id


def distribute_via_treekem(
    key_id: str,
    group_id: str,
    removed_peer_shared_id: str,
    all_members: list[str],
    peer_id: str,
    peer_shared_id: str,
    t_ms: int,
    db: Any,
) -> tuple[list[str], set[str]]:
    """Distribute a key using TreeKEM to cover removed member's copath.

    Args:
        key_id: The group key to distribute
        group_id: The group
        removed_peer_shared_id: The removed member's peer_shared_id
        all_members: List of remaining members' peer_shared_ids
        peer_id: Local peer ID
        peer_shared_id: Public peer ID
        t_ms: Timestamp
        db: Database connection

    Returns:
        Tuple of (key_shared_ids, covered_members) where:
        - key_shared_ids: List of treekem_key_shared event IDs created
        - covered_members: Set of peer_shared_ids covered by TreeKEM distribution
    """
    from events.group import treekem_pubkey_shared

    log.info(f"distribute_via_treekem() key={key_id[:20]}..., removed={removed_peer_shared_id[:20]}...")

    # For all_users_group, signed_by = network_id. Pubkeys are stored by network_id.
    safedb = create_safe_db(db, recorded_by=peer_id)
    group_row = safedb.query_one(
        "SELECT signed_by FROM groups WHERE group_id = ? AND recorded_by = ?",
        (group_id, peer_id)
    )
    network_id = group_row['signed_by'] if group_row and group_row['signed_by'] else group_id

    # Get valid nodes for this network (pubkeys are stored by network_id)
    valid_nodes = treekem_pubkey_shared.get_valid_nodes_for_group(network_id, peer_id, t_ms, db)
    log.info(f"distribute_via_treekem() found {len(valid_nodes)} valid nodes")

    # Find optimal covering nodes
    selected_nodes, covered_members = treekem.get_covering_nodes_for_removal(
        removed_peer_shared_id=removed_peer_shared_id,
        members=all_members,
        valid_nodes=valid_nodes,
    )
    log.info(f"distribute_via_treekem() selected {len(selected_nodes)} nodes covering {len(covered_members)} members")

    # Create key_shared events for each selected node
    key_shared_ids = []
    for i, (depth, path_prefix) in enumerate(selected_nodes):
        # Get the pubkey for this node (use network_id since pubkeys are stored by network_id)
        pubkey_info = treekem_pubkey_shared.get_pubkey_for_node(
            group_id=network_id,
            depth=depth,
            path_prefix=path_prefix,
            peer_id=peer_id,
            t_ms=t_ms,
            db=db,
        )

        if not pubkey_info:
            log.warning(f"distribute_via_treekem() no pubkey for node depth={depth}")
            continue

        try:
            key_shared_id = create(
                key_id=key_id,
                group_id=group_id,
                depth=depth,
                path_prefix=path_prefix,
                recipient_pubkey_id=pubkey_info['pubkey_shared_id'],
                recipient_public_key=pubkey_info['public_key'],
                peer_id=peer_id,
                peer_shared_id=peer_shared_id,
                t_ms=t_ms + i,
                db=db,
            )
            key_shared_ids.append(key_shared_id)
        except Exception as e:
            log.warning(f"distribute_via_treekem() failed to create key_shared for node depth={depth}: {e}")

    log.info(f"distribute_via_treekem() created {len(key_shared_ids)} treekem_key_shared events")
    return key_shared_ids, covered_members


def count_for_group(group_id: str, peer_id: str, db: Any) -> int:
    """Count treekem_key_shared events for a group.

    Args:
        group_id: The group
        peer_id: Local peer ID
        db: Database connection

    Returns:
        Number of treekem_key_shared events
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    row = safedb.query_one(
        "SELECT COUNT(*) as count FROM treekem_keys_shared WHERE group_id = ? AND recorded_by = ?",
        (group_id, peer_id)
    )

    return row['count'] if row else 0


def get_key_sharing_stats(group_id: str, peer_id: str, db: Any) -> dict[str, Any]:
    """Get key sharing statistics for a group.

    Args:
        group_id: The group
        peer_id: Local peer ID
        db: Database connection

    Returns:
        Dict with:
        - group_key_shared_count: int  # Total O(n) shares
        - treekem_key_shared_count: int  # Total O(log n) shares
        - members: int  # Group member count
        - treekem_enabled: bool
    """
    from events.identity import network_settings
    from events.group import group_key_shared

    safedb = create_safe_db(db, recorded_by=peer_id)

    # Count group_key_shared events
    gks_row = safedb.query_one(
        "SELECT COUNT(*) as count FROM group_keys_shared WHERE recorded_by = ?",
        (peer_id,)
    )
    gks_count = gks_row['count'] if gks_row else 0

    # Count treekem_key_shared events
    tks_count = count_for_group(group_id, peer_id, db)

    # Count members
    members_row = safedb.query_one(
        "SELECT COUNT(DISTINCT user_id) as count FROM group_members WHERE group_id = ? AND recorded_by = ?",
        (group_id, peer_id)
    )
    members = members_row['count'] if members_row else 0

    # Check if TreeKEM is enabled
    # For all_users_group, signed_by = network_id, so look that up
    group_row = safedb.query_one(
        "SELECT signed_by FROM groups WHERE group_id = ? AND recorded_by = ?",
        (group_id, peer_id)
    )
    network_id = group_row['signed_by'] if group_row else group_id
    treekem_enabled = network_settings.is_treekem_enabled(network_id, peer_id, db)

    return {
        'group_key_shared_count': gks_count,
        'treekem_key_shared_count': tks_count,
        'members': members,
        'treekem_enabled': treekem_enabled,
    }


def get_network_key_stats(peer_id: str, db: Any) -> dict[str, Any]:
    """Get key sharing stats across all groups for a peer.

    Args:
        peer_id: Local peer ID
        db: Database connection

    Returns:
        Dict with:
        - total_group_key_shared: int  # Total O(n) shares across all groups
        - total_treekem_key_shared: int  # Total O(log n) shares
        - total_members: int  # Total members across all groups
        - groups_with_treekem: int  # Number of groups with TreeKEM enabled
    """
    from events.identity import network_settings

    safedb = create_safe_db(db, recorded_by=peer_id)

    # Count total group_key_shared events
    gks_row = safedb.query_one(
        "SELECT COUNT(*) as count FROM group_keys_shared WHERE recorded_by = ?",
        (peer_id,)
    )
    total_gks = gks_row['count'] if gks_row else 0

    # Count total treekem_key_shared events
    tks_row = safedb.query_one(
        "SELECT COUNT(*) as count FROM treekem_keys_shared WHERE recorded_by = ?",
        (peer_id,)
    )
    total_tks = tks_row['count'] if tks_row else 0

    # Count total unique members
    members_row = safedb.query_one(
        "SELECT COUNT(DISTINCT user_id) as count FROM group_members WHERE recorded_by = ?",
        (peer_id,)
    )
    total_members = members_row['count'] if members_row else 0

    # Count groups with TreeKEM enabled
    treekem_row = safedb.query_one(
        """SELECT COUNT(DISTINCT network_id) as count FROM network_settings
           WHERE recorded_by = ? AND treekem_enabled = 1""",
        (peer_id,)
    )
    groups_with_treekem = treekem_row['count'] if treekem_row else 0

    return {
        'total_group_key_shared': total_gks,
        'total_treekem_key_shared': total_tks,
        'total_members': total_members,
        'groups_with_treekem': groups_with_treekem,
    }
