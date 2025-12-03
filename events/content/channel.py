"""Channel event type (shareable, encrypted).

Pure functions:
    project(input_dict) -> ProjectorResult

API functions:
    create(name, peer_id, peer_shared_id, t_ms, db, ...) -> str
    project_event(event_id, recorded_by, recorded_at, db) -> None
    list_channels(recorded_by, db) -> list
"""
from typing import Any, TypedDict, NotRequired
import json
import logging
import crypto
import store
from events.group import group_key, group as group_module, group_member
from events.identity import peer
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


# ============================================================================
# TYPES
# ============================================================================

class ChannelEventData(TypedDict):
    type: str
    name: str
    group_id: str
    signed_by: str
    created_at: int
    disappearing_time_ms: NotRequired[int]
    is_main: NotRequired[int]
    admin_grant: NotRequired[str]


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": True,
    "signer_type": "peer_shared",
    "dependencies": ["signer_user:linked_peer", "admin_grant:admin_grant?"],
    "tables": ["channels"],
    "generic_dispatch": True,
}


# ============================================================================
# PURE FUNCTIONS
# ============================================================================

def project(input_dict: dict):
    """Pure projection: dict -> result. No database access."""
    from projection import ProjectorResult

    event_id = input_dict["event_id"]
    event_data = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]
    deps = input_dict.get("dependencies", {})

    # Get admin_grant from event
    admin_grant_id = event_data.get("admin_grant")

    if admin_grant_id:
        # Verify admin_grant authorizes the signer
        signer_user = deps.get("signer_user")
        if not signer_user:
            return ProjectorResult(blocked=True, missing_deps=["signer_user"])

        admin_grant = deps.get("admin_grant")
        if not admin_grant:
            return ProjectorResult(blocked=True, missing_deps=["admin_grant"])

        if admin_grant["user_id"] != signer_user["user_id"]:
            return ProjectorResult(
                valid=False,
                reason=f"admin_grant {admin_grant_id} does not authorize signer {signer_user['user_id']}"
            )

    channel_row = {
        "channel_id": event_id,
        "name": event_data.get("name"),
        "group_id": event_data.get("group_id"),
        "signed_by": event_data.get("signed_by"),
        "created_at": event_data.get("created_at"),
        "disappearing_time_ms": event_data.get("disappearing_time_ms", 0),
        "is_main": event_data.get("is_main", 0),
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
    }

    return ProjectorResult(valid=True, tables={"channels": [channel_row]})


# ============================================================================
# TEST BUILDERS
# ============================================================================

def make_event_data(
    name: str = "general",
    group_id: str = "grp_123",
    signed_by: str = "peer_123",
    created_at: int = 1000000,
    disappearing_time_ms: int = 0,
    is_main: int = 0,
    admin_grant: str | None = None,
) -> dict:
    """Build event_data for testing."""
    data = {
        "type": "channel",
        "name": name,
        "group_id": group_id,
        "signed_by": signed_by,
        "created_at": created_at,
        "disappearing_time_ms": disappearing_time_ms,
        "is_main": is_main,
    }
    if admin_grant:
        data["admin_grant"] = admin_grant
    return data


def make_input(
    event_id: str = "ch_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_456",
    recorded_at: int = 1000001,
    signer_user: dict | None = None,
    admin_grant: dict | None = None,
) -> dict:
    """Build complete input dict for testing."""
    return {
        "event_id": event_id,
        "event_data": event_data or make_event_data(),
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "dependencies": {
            "signer_user": signer_user,
            "admin_grant": admin_grant,
        },
    }


# ============================================================================
# API FUNCTIONS
# ============================================================================


def _validate_admin(peer_shared_id: str, recorded_by: str, db: Any) -> bool:
    """Validate that peer_shared_id is an admin.

    An admin is a member of the network's admins group OR the first_peer (network creator).
    Uses centralized is_admin() function from events.identity.invite.

    Args:
        peer_shared_id: Public peer ID to check
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        True if user is admin, False otherwise
    """
    from events.identity import invite
    return invite.is_admin(peer_shared_id, recorded_by, db)


def create(name: str, peer_id: str, peer_shared_id: str, t_ms: int, db: Any,
           group_id: str | None = None, member_user_ids: list[str] | None = None,
           key_id: str | None = None, is_main: bool = False,
           disappearing_time_ms: int = 0, admin_grant: str | None = None) -> str:
    """Create a shareable, encrypted channel event.

    Only admins can create channels (except during initial network setup where group_id is explicitly provided).
    Channels can be:
    - Public: belong to the main group (if group_id=None, will use main group)
    - Private: belong to a dedicated group with specified members (if member_user_ids provided)

    Note: peer_id (local) signs and sees the event; peer_shared_id (public) is the creator identity.

    SECURITY: This function trusts that peer_id is correct and owned by the caller.
    In production, the API authentication layer should validate that the authenticated session
    owns this peer_id before calling this function. This is safe for local-only apps where
    the user controls all peers on the device.

    Args:
        name: Channel name
        peer_id: Local peer ID (for signing and seeing)
        peer_shared_id: Public peer ID (for created_by)
        t_ms: Timestamp
        db: Database connection
        group_id: Optional explicit group ID (if None and no member_user_ids, will use main group)
        member_user_ids: If provided, create private channel for these members (creates new group)
        key_id: Optional explicit key_id (only used if group_id provided, otherwise derived)
        is_main: True if this is the main channel for a group
        disappearing_time_ms: Messages expire after this duration in ms (0 = permanent, >0 = milliseconds)
        admin_grant: Optional admin_id that grants authority to create channels.
                    If provided, used directly. If None, looked up from admins table.

    Returns:
        channel_id: The created channel event ID
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    # Validate disappearing_time_ms
    if disappearing_time_ms < 0:
        raise ValueError("disappearing_time_ms must be non-negative")

    # Check authorization - only admins can create channels
    admin_grant_id = admin_grant  # Use passed-in value if provided

    if not _validate_admin(peer_shared_id, peer_id, db):
        raise ValueError(f"User {peer_shared_id} not authorized to create channels (only admins can)")

    # Get admin_grant for inclusion in event (REQUIRED for convergence)
    # This is needed whether group_id is explicit or not
    if not admin_grant_id:
        # Look up admin_grant from admins table
        from events.identity import admin as admin_module

        # Get user_id for this peer_shared_id
        creator_user_id = None
        peer_row = safedb.query_one(
            "SELECT user_id FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ? LIMIT 1",
            (peer_shared_id, peer_id)
        )
        if peer_row and peer_row['user_id']:
            creator_user_id = peer_row['user_id']

        if creator_user_id:
            # Get network_id for admin lookup
            network_row = safedb.query_one(
                "SELECT network_id FROM networks WHERE recorded_by = ? LIMIT 1",
                (peer_id,)
            )
            if network_row:
                admin_grant_id = admin_module.my_grant(creator_user_id, network_row['network_id'], peer_id, db)

    if admin_grant_id:
        log.info(f"channel.create() including admin_grant={admin_grant_id[:20]}...")
    else:
        log.warning(f"channel.create() NO admin_grant found - event may fail projection on receivers!")

    # Determine group_id and key_id
    if member_user_ids:
        # Private channel: create a new group with specified members
        log.info(f"channel.create() creating PRIVATE channel name='{name}', peer_id={peer_id}, members={len(member_user_ids)}")

        # Create group for the private channel
        private_group_id, private_key_id = group_module.create(
            name=f"private_channel_{name}",
            peer_id=peer_id,
            peer_shared_id=peer_shared_id,
            t_ms=t_ms,
            db=db,
            is_main=False
        )

        group_id = private_group_id
        key_id = private_key_id

        # First, ensure the creator is added to the private channel group
        # Get creator's user_id from peers_shared (user→peer relationship stored there)
        peer_row = safedb.query_one(
            "SELECT user_id FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ? LIMIT 1",
            (peer_shared_id, peer_id)
        )
        if not peer_row or not peer_row['user_id']:
            raise ValueError(f"Creator user not found for peer_shared_id {peer_shared_id}")

        creator_user_id = peer_row['user_id']

        # Track which users we've already added
        added_user_ids = set()
        member_timestamp = t_ms + 10  # Space out timestamps to avoid collisions

        # Add creator first
        try:
            group_member.create(
                group_id=group_id,
                user_id=creator_user_id,
                peer_id=peer_id,
                peer_shared_id=peer_shared_id,
                t_ms=member_timestamp,
                db=db
            )
            log.info(f"channel.create() added creator {creator_user_id} to private channel group")
            added_user_ids.add(creator_user_id)
            member_timestamp += 10
        except Exception as e:
            log.warning(f"channel.create() warning: could not add creator {creator_user_id}: {e}")

        # Add specified members to the private channel group
        # Members should already have prekeys available since they're part of the network
        for user_id in member_user_ids:
            if user_id in added_user_ids:
                continue  # Skip if already added
            try:
                group_member.create(
                    group_id=group_id,
                    user_id=user_id,
                    peer_id=peer_id,
                    peer_shared_id=peer_shared_id,
                    t_ms=member_timestamp,
                    db=db
                )
                log.info(f"channel.create() added member {user_id} to private channel group")
                added_user_ids.add(user_id)
                member_timestamp += 10
            except Exception as e:
                log.warning(f"channel.create() warning: could not immediately add member {user_id} (may need key sharing later): {e}")
                # Don't raise - members can be added later via add_member_to_channel()

        # Add all admins to the private channel group
        # Admins should be in the network and have prekeys
        admins = _get_admin_user_ids(peer_id, db)
        for admin_user_id in admins:
            if admin_user_id in added_user_ids:
                continue  # Skip if already added
            try:
                group_member.create(
                    group_id=group_id,
                    user_id=admin_user_id,
                    peer_id=peer_id,
                    peer_shared_id=peer_shared_id,
                    t_ms=member_timestamp,
                    db=db
                )
                log.info(f"channel.create() added admin {admin_user_id} to private channel group")
                added_user_ids.add(admin_user_id)
                member_timestamp += 10
            except Exception as e:
                log.warning(f"channel.create() warning: could not add admin {admin_user_id} (may need key sharing later): {e}")
    else:
        # Public channel: use main group
        if not group_id:
            # Find the main group
            main_group = safedb.query_one(
                "SELECT group_id, key_id FROM groups WHERE is_main = 1 AND recorded_by = ? LIMIT 1",
                (peer_id,)
            )
            if not main_group:
                raise ValueError("No main group found - cannot create public channel")
            group_id = main_group['group_id']
            key_id = main_group['key_id']
        elif not key_id:
            # If group_id provided but no key_id, look it up
            group_row = safedb.query_one(
                "SELECT key_id FROM groups WHERE group_id = ? AND recorded_by = ?",
                (group_id, peer_id)
            )
            if not group_row:
                raise ValueError(f"Group {group_id} not found")
            key_id = group_row['key_id']

        log.info(f"channel.create() creating PUBLIC channel name='{name}', group_id={group_id}, peer_id={peer_id}, is_main={is_main}")

    # Create event dict
    event_data = {
        'type': 'channel',
        'name': name,
        'group_id': group_id,
        'signed_by': peer_shared_id,  # References shareable peer identity
        'created_at': t_ms,
        'disappearing_time_ms': disappearing_time_ms,  # Store disappearing time
        'is_main': 1 if is_main else 0  # Store is_main flag
    }

    # Include admin_grant for projection-time verification (if available)
    if admin_grant_id:
        event_data['admin_grant'] = admin_grant_id

    # Sign the event with local peer's private key
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Get key_data for encryption
    key_data = group_key.get_key(key_id, peer_id, db)

    # Wrap (canonicalize + encrypt)
    canonical = crypto.canonicalize_json(signed_event)
    blob = crypto.wrap(canonical, key_data, db)

    # Store event with recorded wrapper and projection
    event_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"channel.create() created channel_id={event_id}")
    return event_id


def _get_admin_user_ids(recorded_by: str, db: Any) -> list[str]:
    """Get all admin user IDs from the admins table.

    Uses the event-sourced admins table rather than group membership.

    Args:
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        List of admin user_ids
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Query admins table directly (event-sourced admin grants)
    admin_rows = safedb.query(
        "SELECT DISTINCT user_id FROM admins WHERE recorded_by = ?",
        (recorded_by,)
    )

    return [row['user_id'] for row in admin_rows]


def project_event(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Project channel event into channels table and shareable_events table."""
    log.warning(f"[CHANNEL_PROJECT_START] channel.project() CALLED for channel_id={event_id[:20]}... recorded_by={recorded_by[:20]}... recorded_at={recorded_at}")

    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    blob = store.get(event_id, unsafedb)
    if not blob:
        log.warning(f"channel.project() blob not found for channel_id={event_id}")
        log.warning(f"[ASSERT] channel.project() blob must exist before projection - FAILING!")
        raise RuntimeError(f"[CHANNEL_PROJECT_ERROR] blob not found for channel_id={event_id}")

    # Unwrap (decrypt)
    unwrapped, _ = crypto.unwrap(blob, recorded_by, db)
    if not unwrapped:
        log.warning(f"channel.project() unwrap failed for channel_id={event_id}")
        log.warning(f"[ASSERT] channel.project() unwrap must succeed before projection - FAILING!")
        raise RuntimeError(f"[CHANNEL_PROJECT_ERROR] unwrap failed for channel_id={event_id}, recorded_by={recorded_by[:20]}...")

    # Parse JSON
    event_data = crypto.parse_json(unwrapped)
    log.warning(f"[CHANNEL_PROJECT] ✓ channel_id={event_id[:20]}... name='{event_data.get('name')}' recorded_by={recorded_by[:20]}...")

    # Check: Verify all required fields exist
    assert event_data.get('name'), f"[CHANNEL_ASSERT] channel must have name"
    assert event_data.get('group_id'), f"[CHANNEL_ASSERT] channel must have group_id"
    assert event_data.get('signed_by'), f"[CHANNEL_ASSERT] channel must have signed_by"
    assert isinstance(event_data.get('created_at'), int), f"[CHANNEL_ASSERT] channel must have created_at as int, got {type(event_data.get('created_at'))}"
    log.warning(f"[CHANNEL_ASSERT] ✓ All required fields present for channel {event_id[:20]}...")

    # Verify signature - get public key from signed_by peer_shared
    from events.identity import peer_shared
    signed_by = event_data['signed_by']
    public_key = peer_shared.get_public_key(signed_by, recorded_by, db)
    if not crypto.verify_event(event_data, public_key):
        log.warning(f"channel.project() signature verification FAILED for channel_id={event_id}")
        return  # Reject unsigned or invalid signature

    # Verify admin authorization via admin_grant chain
    # Channels require admin_grant unless they're bootstrap channels (no admin_grant field)
    admin_grant = event_data.get('admin_grant')
    if admin_grant:
        # Verify signer is authorized by admin_grant
        # Get signer's user_id from peers_shared (user→peer relationship stored there)
        peer_row = safedb.query_one(
            "SELECT user_id FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ?",
            (signed_by, recorded_by)
        )
        if not peer_row or not peer_row['user_id']:
            log.warning(f"channel.project() signer user not found for {signed_by[:20]}...")
            return  # Dependency not ready

        signer_user_id = peer_row['user_id']

        # Verify admin_grant references an admin event for signer's user
        grant_row = safedb.query_one(
            "SELECT user_id FROM admins WHERE admin_id = ? AND recorded_by = ?",
            (admin_grant, recorded_by)
        )
        if not grant_row:
            log.warning(f"channel.project() admin_grant {admin_grant[:20]}... not found - dependency not ready")
            return  # Dependency not ready
        if grant_row['user_id'] != signer_user_id:
            log.warning(f"channel.project() admin_grant {admin_grant[:20]}... does not authorize signer {signer_user_id[:20]}...")
            return  # Invalid authorization

        log.info(f"channel.project() admin authorization verified for channel {event_id[:20]}...")

    # Insert into channels table (use REPLACE to overwrite stubs from user.project())
    log.warning(f"[CHANNEL_PROJECT] Inserting into channels table: channel_id={event_id[:20]}... recorded_by={recorded_by[:20]}... recorded_at={recorded_at}")

    # Before insert, check current state
    existing = safedb.query(
        "SELECT * FROM channels WHERE channel_id = ? AND recorded_by = ?",
        (event_id, recorded_by)
    )
    if existing:
        log.warning(f"[CHANNEL_INSERT_REPLACE] Already exists: {len(existing)} rows for channel_id={event_id[:20]}... recorded_by={recorded_by[:20]}...")

    safedb.execute(
        """INSERT OR REPLACE INTO channels
           (channel_id, name, group_id, signed_by, created_at, disappearing_time_ms, is_main, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event_id,
            event_data['name'],
            event_data['group_id'],
            event_data['signed_by'],
            event_data['created_at'],
            event_data.get('disappearing_time_ms', 0),  # Default to 0 if not present (backward compatibility)
            event_data.get('is_main', 0),  # Default to 0 if not present (backward compatibility)
            recorded_by,
            recorded_at
        )
    )

    # After insert, verify it was actually inserted
    after_insert = safedb.query(
        "SELECT * FROM channels WHERE channel_id = ? AND recorded_by = ?",
        (event_id, recorded_by)
    )
    if after_insert:
        log.warning(f"[CHANNEL_PROJECT_INSERTED] ✓ Row inserted for channel_id={event_id[:20]}... recorded_by={recorded_by[:20]}... recorded_at={recorded_at} ({len(after_insert)} total rows)")
    else:
        log.warning(f"[CHANNEL_INSERT_FAILED] ✗ Row NOT found after insert! channel_id={event_id[:20]}... recorded_by={recorded_by[:20]}...")
        # This is a major bug - the insert failed silently
        assert False, f"[CHANNEL_ASSERT] Insert failed for channel {event_id[:20]}... recorded_by={recorded_by[:20]}..."


def list_channels(recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """List all channels for a specific peer."""
    safedb = create_safe_db(db, recorded_by=recorded_by)
    return safedb.query(
        """SELECT channel_id, name, group_id, signed_by, created_at, disappearing_time_ms
           FROM channels
           WHERE recorded_by = ?
           ORDER BY created_at DESC""",
        (recorded_by,)
    )


def add_member_to_channel(channel_id: str, user_id: str, peer_id: str, peer_shared_id: str, t_ms: int, db: Any) -> str:
    """Add a member to a private channel (admin-only).

    This adds the user as a member of the channel's underlying group, giving them
    access to decrypt messages in that channel.

    Args:
        channel_id: Channel to add member to
        user_id: User (peer_shared_id) to add
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (for created_by)
        t_ms: Timestamp
        db: Database connection

    Returns:
        member_id: The created group_member event ID

    Raises:
        ValueError: If channel not found, user not admin, or operation fails
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    # Check authorization - only admins can add members
    if not _validate_admin(peer_shared_id, peer_id, db):
        raise ValueError(f"User {peer_shared_id} not authorized to add members (only admins can)")

    # Get the channel to find its group_id
    channel = safedb.query_one(
        "SELECT group_id FROM channels WHERE channel_id = ? AND recorded_by = ?",
        (channel_id, peer_id)
    )
    if not channel:
        raise ValueError(f"Channel {channel_id} not found")

    group_id = channel['group_id']

    log.info(f"add_member_to_channel() adding user={user_id} to channel={channel_id}, group={group_id}")

    # Add the user as a member of the group
    member_id = group_member.create(
        group_id=group_id,
        user_id=user_id,
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        t_ms=t_ms,
        db=db
    )

    log.info(f"add_member_to_channel() added member_id={member_id}")
    return member_id
