"""User removed event type - marks a user as removed from a network."""

# Registry metadata
EVENT_TYPE = 'user_removed'
SHAREABLE = True  # User removal syncs across network
PROJECTION_TABLE = None

# Wire format constants
WIRE_TYPE_CODE = 0x22  # TYPE_USER_REMOVED
WIRE_PLAINTEXT_SIZE = 344  # USER_REMOVED_PLAINTEXT_SIZE

from typing import Any
import logging
import struct
from core import crypto
from core import store
from core import wire_format
from core.db import create_safe_db, create_unsafe_db
from core.projection.types import ProjectorResult, WriteOp, Command
from core.projection.apply import register_command_handler
from events.identity import user, peer_shared, network

log = logging.getLogger(__name__)


# Wire format functions - encode/decode for user_removed event type

def encode_plaintext(removed_user_id: bytes) -> bytes:
    """Encode a user_removed payload plaintext (pre-encryption).

    Layout (344 bytes):
    - removed_user_id (16)
    - pad (328)
    """
    wire_format._require_len("removed_user_id", removed_user_id, 16)
    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:16] = removed_user_id
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a user_removed payload plaintext (post-decryption)."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(
            f"user_removed plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    return {"removed_user_id": data[0:16]}


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a user_removed wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(
    *,
    removed_user_id_b64: str,
    removed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    private_key: bytes,
) -> bytes:
    """Encode a complete user_removed wire event."""
    removed_user_id = crypto.b64decode(removed_user_id_b64)
    signer_id = crypto.b64decode(removed_by_b64)
    plaintext = encode_plaintext(removed_user_id=removed_user_id)
    header = wire_format.WireHeader(
        version=1,
        event_type=WIRE_TYPE_CODE,
        flags=0,
        signer_type=wire_format.signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=wire_format._require_len("signer_id", signer_id, wire_format.SIGNER_ID_SIZE),
    )
    signed_bytes = wire_format._signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = wire_format._pad_payload(plaintext)
    return wire_format.build_envelope(header, payload, signature)


def decode_wire_event(data: bytes) -> dict[str, Any]:
    """Decode a user_removed wire event."""
    header, payload, signature = wire_format.parse_envelope(data)
    if header.event_type != WIRE_TYPE_CODE:
        raise ValueError("unexpected event type for user_removed")
    plaintext = payload[:WIRE_PLAINTEXT_SIZE]
    decoded = decode_plaintext(plaintext)
    event_data = {
        "type": EVENT_TYPE,
        "removed_user_id": crypto.b64encode(decoded["removed_user_id"]),
        "removed_by": crypto.b64encode(header.signer_id),
        "signer_type": wire_format.signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": wire_format._signing_bytes(header, plaintext),
    }
    return event_data


# V2 Projector specification
EVENT_SPEC = {
    'encrypted': False,
    'signer': {
        'id_field': 'removed_by',
        'type_field': 'signer_type',
    },
    'requires': {
        'signer_peer_shared': {
            'source': 'table',
            'table': 'peers_shared',
            'key': 'peer_shared_id',
            'key_from': 'removed_by',
            'fields': ['peer_shared_id', 'public_key'],
        },
    },
    'optional': {},
}


def validate(removed_user_id: str, removed_by_peer_id: str, recorded_by: str, db: Any) -> bool:
    """Validate that removed_by has authorization to remove the user.

    Authorization rule:
    - User can remove themselves (via any linked peer)
    - Admin can remove any user

    Args:
        removed_user_id: User ID being removed
        removed_by_peer_id: peer_shared_id removing the user
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        True if authorized, False otherwise
    """
    # Get removed_by's user_id
    removed_by_user_id = peer_shared.get_user_id(removed_by_peer_id, recorded_by, db)
    if not removed_by_user_id:
        return False

    # Rule 1: User can remove themselves
    if removed_by_user_id == removed_user_id:
        return True

    # Rule 2: Admin can remove any user
    # Use centralized is_admin() function which handles both normal admins and first_peer
    from events.identity import invite
    return invite.is_admin(removed_by_peer_id, recorded_by, db)


def create(removed_user_id: str, removed_by_peer_id: str, removed_by_local_peer_id: str, t_ms: int, db: Any) -> dict[str, Any]:
    """Create a user_removed event.

    Args:
        removed_user_id: User ID to remove
        removed_by_peer_id: peer_shared_id of remover (for event data)
        removed_by_local_peer_id: Local peer ID of remover (for signing)
        t_ms: Timestamp
        db: Database connection

    Returns:
        dict with:
        - event_id: ID of the created user_removed event
        - removed_user_name: Name of the removed user (for display)
        - members: Updated list of group members (excluding the removed user)
    """
    # Get removed user's name for the return value (before removal)
    removed_user_name = user.get_display_name(removed_user_id, removed_by_local_peer_id, db) or '???'

    # Validate authorization
    if not validate(removed_user_id, removed_by_peer_id, removed_by_local_peer_id, db):
        raise ValueError("Not authorized to remove this user")

    _wire_shadow_user_removed(removed_user_id)

    from events.identity import peer
    private_key = peer.get_private_key(removed_by_local_peer_id, removed_by_local_peer_id, db)
    blob = encode_wire_event(
        removed_user_id_b64=removed_user_id,
        removed_by_b64=removed_by_peer_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        private_key=private_key,
    )
    event_id = store.event(blob, removed_by_local_peer_id, t_ms, db)

    # Get updated member list after removal (for immediate UI feedback)
    from events.group import group_member

    # Get network_id to find all_users group
    network_id = network.get_network_id(removed_by_local_peer_id, db)
    members = []
    if network_id:
        all_users_group_id = network.get_all_users_group_id(
            network_id, removed_by_local_peer_id, db
        )
        if all_users_group_id:
            members = group_member.list_members(all_users_group_id, removed_by_local_peer_id, db)

    return {
        'event_id': event_id,
        'removed_user_name': removed_user_name,
        'members': members
    }


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for user_removed - returns writes without side effects.

    Core projection: insert into removed_users table.
    Side effects (peer cascade, connection removal, key rotation) handled in recorded.py.
    """
    event_data = ctx.event_data
    recorded_by = ctx.recorded_by

    removed_user_id = event_data.get('removed_user_id')
    removed_at = event_data.get('created_at')
    signed_by = event_data.get('removed_by')
    signer = ctx.signer or {}
    signer_user_id = signer.get('user_id')
    signer_is_admin = signer.get('is_admin')

    if not removed_user_id:
        log.warning(f"user_removed.project_pure() missing removed_user_id")
        return ProjectorResult(writes=tuple(), valid_event=False)

    if not signed_by:
        log.warning(f"user_removed.project_pure() missing removed_by field")
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_user_removed(removed_user_id)

    if not signer_user_id:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Authorization: self-removal or admin removal
    if signer_user_id != removed_user_id and not signer_is_admin:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Core write: insert into removed_users table (scoped by recorded_by)
    # recorded_at = when THIS peer stored the event (tick time), used for
    # split-brain detection to know when the peer became AWARE of the removal
    writes = [
        WriteOp(
            op='insert',
            table='removed_users',
            values={
                'user_id': removed_user_id,
                'removed_at': removed_at,
                'signed_by': signed_by,
                'recorded_by': recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
    ]

    # Side effects: cascade peer removal, delete connections, rotate keys
    commands = (
        Command(
            command_type='handle_user_removed_side_effects',
            args={
                'removed_user_id': removed_user_id,
                'removed_at': removed_at,
                'removed_by': signed_by,
            }
        ),
    )

    return ProjectorResult(writes=tuple(writes), valid_event=True, commands=commands)


def _wire_shadow_user_removed(removed_user_id: str) -> None:
    """Validate user_removed fields against the fixed-size wire payload layout."""
    plaintext = encode_plaintext(
        removed_user_id=crypto.b64decode(removed_user_id),
    )
    decoded = decode_plaintext(plaintext)
    if decoded["removed_user_id"] != crypto.b64decode(removed_user_id):
        raise ValueError("wire shadow decode removed_user_id mismatch")


def _handle_user_removed_side_effects(
    removed_user_id: str, removed_at: int, removed_by: str, recorded_by: str,
    recorded_at: int, db: Any
) -> None:
    """Handle side effects of user removal.

    Called from recorded.py's post-projection hook via the command handler.

    Side effects:
    1. Cascade: Mark all peers of the removed user as removed
    2. Delete connections for each of those peers
    3. Rotate group keys (only if this peer created the event)
    4. Split-brain detection and re-rotation
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    unsafe_db = create_unsafe_db(db)

    # Cascade: Find all peers for this user from peers_shared and mark them as removed
    peers = safedb.query(
        "SELECT peer_shared_id FROM peers_shared WHERE user_id = ? AND recorded_by = ?",
        (removed_user_id, recorded_by)
    )

    for peer_row in peers:
        peer_shared_id = peer_row['peer_shared_id']
        # Mark peer as removed in device-wide table by peer_shared_id
        unsafe_db.execute(
            """INSERT OR IGNORE INTO removed_peers (peer_shared_id, removed_at, signed_by)
               VALUES (?, ?, ?)""",
            (peer_shared_id, removed_at, removed_by)
        )

        # DELETE ALL CONNECTIONS for this peer (enforcement mechanism)
        from events.network import connection_request
        deleted_count = connection_request.remove_connections_for_peer(peer_shared_id, db)
        log.info(f"user_removed: deleted {deleted_count} connection(s) for peer {peer_shared_id[:20]}...")

    # Also remove bootstrap connections for the specific invite the removed user used to join.
    # Bootstrap connections have peer_shared_id IS NULL so remove_connections_for_peer misses them.
    # We read the user event blob to find the invite_id (the user invite used to join).
    user_blob = store.get(removed_user_id, unsafe_db)
    if user_blob:
        try:
            user_event_data = user.decode_wire_event(user_blob)
            bootstrap_invite_id = user_event_data.get('invite_id')
            if bootstrap_invite_id:
                from events.network import connection_request as cr
                bootstrap_deleted = cr.remove_connections_for_invite(bootstrap_invite_id, db)
                if bootstrap_deleted:
                    log.info(f"user_removed: deleted {bootstrap_deleted} bootstrap connection(s) for invite {bootstrap_invite_id[:20]}...")
        except Exception as e:
            log.warning(f"user_removed: failed to read user event blob for bootstrap cleanup: {e}")

    # Invalidate all open invites (removed user may know these)
    _invalidate_invites_for_removal(removed_user_id, recorded_by, removed_at, db)

    # Rotate group keys for all groups this user was a member of
    # IMPORTANT: Only the peer who CREATED this event should rotate keys.
    # Other peers receive the rotated keys via group_key_shared.
    peer_self_row = safedb.query_one(
        "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (recorded_by, recorded_by)
    )
    if peer_self_row and peer_self_row['peer_shared_id'] == removed_by:
        # This peer created the user_removed event - they should rotate keys
        _rotate_keys_for_removed_user(removed_user_id, recorded_by, removed_at, db)
    else:
        log.debug(f"user_removed: skipping key rotation - not the event creator")

    # SPLIT-BRAIN DETECTION: Check if current key was shared with any removed user
    # This handles the case where concurrent removals in partitions create keys
    # that don't exclude all removed users
    if peer_self_row:
        _check_and_rerotate_for_split_brain(
            removed_user_id, recorded_by, peer_self_row['peer_shared_id'], recorded_at, db
        )


def _rotate_keys_for_removed_user(removed_user_id: str, recorded_by: str, t_ms: int, db: Any) -> None:
    """Rotate group keys for all groups a removed user was a member of.

    This ensures the removed user cannot decrypt future messages in any group.

    Args:
        removed_user_id: User ID being removed
        recorded_by: Peer ID performing the removal (and key rotation)
        t_ms: Timestamp for key creation
        db: Database connection
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Find all groups this user was a member of
    group_memberships = safedb.query(
        "SELECT DISTINCT group_id FROM group_members WHERE user_id = ? AND recorded_by = ?",
        (removed_user_id, recorded_by)
    )

    if not group_memberships:
        log.info(f"user_removed._rotate_keys_for_removed_user() user {removed_user_id[:20]}... was not a member of any groups")
        return

    log.info(f"user_removed._rotate_keys_for_removed_user() rotating keys for {len(group_memberships)} groups")

    # Get remover's peer_shared_id for signing rotated keys
    peer_self_row = safedb.query_one(
        "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (recorded_by, recorded_by)
    )
    if not peer_self_row:
        log.warning(f"user_removed._rotate_keys_for_removed_user() could not find peer_shared_id for {recorded_by[:20]}..., skipping key rotation")
        return

    peer_shared_id = peer_self_row['peer_shared_id']

    # Look up removed user's peer_shared_id for TreeKEM copath calculation
    removed_peer_row = safedb.query_one(
        "SELECT peer_shared_id FROM peers_shared WHERE user_id = ? AND recorded_by = ? LIMIT 1",
        (removed_user_id, recorded_by)
    )
    removed_peer_shared_id = removed_peer_row['peer_shared_id'] if removed_peer_row else None
    if removed_peer_shared_id:
        log.info(f"user_removed._rotate_keys_for_removed_user() found peer_shared_id for removed user: {removed_peer_shared_id[:20]}...")
    else:
        log.warning(f"user_removed._rotate_keys_for_removed_user() no peer_shared_id found for removed user, TreeKEM will use fallback")

    # Rotate key for each group
    from events.group import group_key, group

    for group_row in group_memberships:
        group_id = group_row['group_id']
        try:
            # IDEMPOTENCY CHECK: Only rotate if we haven't already done so for this removal
            # Check if current key was created BEFORE the removal event
            # If current key was created AFTER, we've already rotated
            current_key = group.get_current_key(group_id, recorded_by, db)
            if current_key:
                # Look up key's created_at from group_keys table
                key_row = safedb.query_one(
                    "SELECT created_at FROM group_keys WHERE key_id = ? AND recorded_by = ?",
                    (current_key['key_id'], recorded_by)
                )
                key_created_at = key_row['created_at'] if key_row else 0
                if key_created_at >= t_ms:
                    log.info(f"user_removed._rotate_keys_for_removed_user() skipping rotation for group {group_id[:20]}... - already rotated")
                    continue

            group_key.rotate_for_removal(
                group_id=group_id,
                peer_id=recorded_by,
                peer_shared_id=peer_shared_id,
                t_ms=t_ms,  # No offset needed - DAG deps handle ordering
                removed_user_id=removed_user_id,
                removed_peer_shared_id=removed_peer_shared_id,
                db=db
            )
            log.info(f"user_removed._rotate_keys_for_removed_user() rotated key for group {group_id[:20]}...")
        except Exception as e:
            log.warning(f"user_removed._rotate_keys_for_removed_user() failed to rotate key for group {group_id[:20]}...: {e}")
            # Continue rotating keys for other groups even if one fails


def _invalidate_invites_for_removal(removed_user_id: str, recorded_by: str, t_ms: int, db: Any) -> int:
    """Invalidate all open invites when a user is removed.

    This prevents the removed user from using known invites to reconnect.
    The removed user's connections are separately cleaned up by
    remove_connections_for_peer() in _handle_user_removed_side_effects().

    Returns count of invites invalidated.
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    unsafedb = create_unsafe_db(db)

    # Find ALL open invites - removed user may know about any of them
    # (they were created while the user was a member)
    invites_to_invalidate = safedb.query(
        "SELECT invite_id FROM invites WHERE recorded_by = ?",
        (recorded_by,)
    )

    total_invalidated = 0

    for inv_row in invites_to_invalidate:
        invite_id = inv_row['invite_id']

        # Mark invite as invalidated (device-wide) so we reject future bootstrap connections
        unsafedb.execute(
            "INSERT OR IGNORE INTO invalidated_invites (invite_id, invalidated_at, reason) VALUES (?, ?, ?)",
            (invite_id, t_ms, 'user_removed')
        )
        # NOTE: We do NOT remove connections here. Bootstrap connections for the specific
        # invite used by the removed user are cleaned up in _handle_user_removed_side_effects.
        # Removing connections for ALL invites would break sync for non-removed members
        # who joined via those same invites and still rely on bootstrap connections.

        total_invalidated += 1

    # Also delete invite records created by the removed user's peers
    removed_peers = safedb.query(
        "SELECT peer_shared_id FROM peers_shared WHERE user_id = ? AND recorded_by = ?",
        (removed_user_id, recorded_by)
    )
    removed_peer_ids = [p['peer_shared_id'] for p in removed_peers]

    if removed_peer_ids:
        placeholders = ",".join("?" for _ in removed_peer_ids)
        safedb.execute(
            f"DELETE FROM invites WHERE recorded_by = ? AND inviter_id IN ({placeholders})",
            (recorded_by, *removed_peer_ids)
        )

    log.info(f"_invalidate_invites_for_removal: invalidated {total_invalidated} invites for removed user {removed_user_id[:20]}...")

    return total_invalidated


def _check_and_rerotate_for_split_brain(
    trigger_removed_user_id: str, recorded_by: str, peer_shared_id: str, t_ms: int, db: Any
) -> None:
    """Check for split-brain key distribution and re-rotate if needed.

    This handles the case where concurrent removals in network partitions create keys
    that don't exclude all removed users. For example:
    - Alice removes Bob -> creates key_A (excludes Bob, but includes Dave!)
    - Carol removes Dave (in partition) -> creates key_C (excludes Dave, but includes Bob!)
    - After partition heals, whichever key "wins" is compromised

    Detection: Check if the current group key was shared with (encrypted TO) any
    removed user via group_key_shared. If so, the key is compromised and needs rotation.

    To avoid competing re-rotations from multiple admins, only the admin with the
    lexicographically lowest peer_shared_id performs the re-rotation.

    Args:
        trigger_removed_user_id: The user_id that triggered this check (just removed)
        recorded_by: Local peer ID
        peer_shared_id: Local peer's shared ID
        t_ms: Timestamp
        db: Database connection
    """
    from events.group import group_key, group
    from events.identity import invite

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Only admins can rotate keys
    if not invite.is_admin(peer_shared_id, recorded_by, db):
        return

    # Split-brain only matters with 2+ removed users
    removed_users = safedb.query(
        "SELECT user_id FROM removed_users WHERE recorded_by = ?",
        (recorded_by,)
    )
    if len(removed_users) < 2:
        return

    removed_user_ids = {r['user_id'] for r in removed_users}

    # Deterministic admin tiebreaker: only the admin with the lowest peer_shared_id
    # re-rotates to prevent competing keys that can't converge.
    network_id = network.get_network_id(recorded_by, db)
    if not network_id:
        return

    all_admin_users = safedb.query(
        "SELECT user_id FROM admins WHERE network_id = ? AND recorded_by = ?",
        (network_id, recorded_by)
    )
    # Get peer_shared_ids for non-removed admins
    active_admin_peer_ids = []
    for au in all_admin_users:
        if au['user_id'] in removed_user_ids:
            continue
        p = safedb.query_one(
            "SELECT peer_shared_id FROM peers_shared WHERE user_id = ? AND recorded_by = ? LIMIT 1",
            (au['user_id'], recorded_by)
        )
        if p:
            active_admin_peer_ids.append(p['peer_shared_id'])

    active_admin_peer_ids.sort()
    if not active_admin_peer_ids or active_admin_peer_ids[0] != peer_shared_id:
        # We're not the designated re-rotation admin
        return

    all_users_group_id = network.get_all_users_group_id(network_id, recorded_by, db)
    if not all_users_group_id:
        return

    # Check if the current key was already rotated after this peer became aware
    # of ALL removals. We compare key_updated_at against MAX(recorded_at) from
    # removed_users - recorded_at is when THIS peer ingested the removal event
    # (not when the remover created it). This prevents infinite re-rotation
    # loops: once a key is created after all removals are known, skip.
    from events.group import group
    group_row = safedb.query_one(
        "SELECT key_updated_at FROM groups WHERE group_id = ? AND recorded_by = ?",
        (all_users_group_id, recorded_by)
    )
    max_recorded_at = safedb.query_one(
        "SELECT MAX(recorded_at) as max_recorded_at FROM removed_users WHERE recorded_by = ?",
        (recorded_by,)
    )
    if group_row and max_recorded_at and group_row['key_updated_at'] >= max_recorded_at['max_recorded_at']:
        log.debug(
            f"user_removed._check_and_rerotate_for_split_brain() key already up-to-date "
            f"(key_updated_at={group_row['key_updated_at']} >= max_recorded_at={max_recorded_at['max_recorded_at']})"
        )
        return

    log.warning(
        f"user_removed._check_and_rerotate_for_split_brain() DETECTED: current key needs "
        f"re-rotation to exclude all {len(removed_users)} removed users"
    )

    try:
        group_key.rotate_for_removal(
            group_id=all_users_group_id,
            peer_id=recorded_by,
            peer_shared_id=peer_shared_id,
            t_ms=t_ms,
            removed_user_id=trigger_removed_user_id,
            db=db
        )
        log.info(f"user_removed._check_and_rerotate_for_split_brain() re-rotated key for split-brain recovery")
    except Exception as e:
        log.warning(f"user_removed._check_and_rerotate_for_split_brain() re-rotation failed: {e}")


def periodic_split_brain_check(recorded_by: str, peer_shared_id: str, t_ms: int, db: Any) -> None:
    """Periodic safety-net check for split-brain key compromise.

    Called by TreeKEMUpdateJob every 5 minutes. If there are 2+ removed users
    and the current key was shared with any of them, triggers re-rotation.

    Args:
        recorded_by: Local peer ID
        peer_shared_id: Local peer's shared ID
        t_ms: Current timestamp
        db: Database connection
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    removed_users = safedb.query(
        "SELECT user_id FROM removed_users WHERE recorded_by = ?",
        (recorded_by,)
    )
    if len(removed_users) < 2:
        return
    # Use the first removed user as trigger; actual key exclusion uses the full
    # removed_users table via LEFT JOIN in rotate_for_removal.
    _check_and_rerotate_for_split_brain(
        removed_users[0]['user_id'], recorded_by, peer_shared_id, t_ms, db
    )


def _command_handle_user_removed_side_effects(args: dict, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Command handler wrapper for _handle_user_removed_side_effects."""
    removed_user_id = args.get('removed_user_id')
    removed_at = args.get('removed_at')
    removed_by = args.get('removed_by')

    if not removed_user_id:
        log.warning("handle_user_removed_side_effects: missing removed_user_id")
        return

    _handle_user_removed_side_effects(removed_user_id, removed_at, removed_by, recorded_by, recorded_at, db)


# Register command handler at module load
register_command_handler('handle_user_removed_side_effects', _command_handle_user_removed_side_effects)
