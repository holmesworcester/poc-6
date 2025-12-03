"""Peer shared event type (shareable public identity).

Pure functions:
    project(input_dict) -> ProjectorResult

API functions:
    create(peer_id, t_ms, db, invite_id, invite_private_key) -> str
    project_event(peer_shared_id, recorded_by, recorded_at, db) -> str | None
    get_public_key(peer_shared_id, recorded_by, db) -> bytes
"""
from typing import Any, TypedDict, NotRequired
import json
import logging
import crypto
import store
from events.identity import peer
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


# ============================================================================
# TYPES
# ============================================================================

class PeerSharedEventData(TypedDict):
    type: str
    public_key: str  # Base64 encoded
    peer_id: str  # Link to local peer
    created_at: int
    invite_id: NotRequired[str]  # For invite-signed mode
    signed_by: NotRequired[str]  # invite_id for invite-signed, omitted for self-signed


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": False,  # Plaintext, signed
    "signer_type": "peer_shared_polymorphic",  # Custom: invite or self
    "dependencies": ["invite:invite?"],  # Optional - only for invite-signed mode
    "tables": ["peers_shared", "peer_self", "valid_events"],
    "generic_dispatch": True,
    "mark_valid": True,
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
    signature_valid = input_dict.get("signature_valid", True)
    deps = input_dict.get("dependencies", {})

    # Validate event type
    if event_data.get("type") != "peer_shared":
        return ProjectorResult(valid=False, reason="Invalid event type")

    # Check required fields
    public_key = event_data.get("public_key")
    if not public_key:
        return ProjectorResult(valid=False, reason="Missing public_key")

    peer_id = event_data.get("peer_id")
    if not peer_id:
        return ProjectorResult(valid=False, reason="Missing peer_id")

    # Determine verification mode
    invite_id = event_data.get("invite_id")
    signed_by = event_data.get("signed_by")
    is_invite_signed = invite_id and signed_by == invite_id

    user_id = None  # Will be set if invite-based

    if is_invite_signed:
        # Invite-signed mode: need invite dependency for user_id
        invite_dep = deps.get("invite")
        if not invite_dep:
            return ProjectorResult(
                blocked=True,
                missing_deps=["invite"],
                reason=f"Waiting for invite {invite_id}"
            )

        # Get user_id from invite
        invite_data = invite_dep.get("event_data", {})
        user_id = invite_data.get("user_id")

        # Signature must be valid (verified by resolver against invite_pubkey)
        if not signature_valid:
            return ProjectorResult(valid=False, reason="Invalid signature (invite key)")
    else:
        # Legacy self-signed mode
        if not signature_valid:
            return ProjectorResult(valid=False, reason="Invalid signature (self-signed)")

    # Build output rows
    tables = {}

    # peers_shared row
    peer_shared_row = {
        "peer_shared_id": event_id,
        "peer_id": peer_id,
        "public_key": public_key,
        "user_id": user_id,  # NULL if self-signed, set if invite-signed
        "created_at": event_data["created_at"],
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
    }
    tables["peers_shared"] = [peer_shared_row]

    # peer_self row - only for own peer AND invite-signed (has user_id)
    # Self-signed bootstrap should NOT update peer_self
    if peer_id == recorded_by and user_id:
        peer_self_row = {
            "peer_id": peer_id,
            "peer_shared_id": event_id,
            "user_id": user_id,
            "recorded_by": recorded_by,
            "recorded_at": recorded_at,
        }
        tables["peer_self"] = [peer_self_row]

    # valid_events row
    valid_event_row = {
        "event_id": event_id,
        "recorded_by": recorded_by,
    }
    tables["valid_events"] = [valid_event_row]

    return ProjectorResult(valid=True, tables=tables)


# ============================================================================
# TEST BUILDERS
# ============================================================================

def make_event_data(
    public_key: str = "pubkey_abc123",
    peer_id: str = "peer_123",
    created_at: int = 1000000,
    invite_id: str = "",
    signed_by: str = "",
) -> dict:
    """Build event_data for testing."""
    result = {
        "type": "peer_shared",
        "public_key": public_key,
        "peer_id": peer_id,
        "created_at": created_at,
    }
    if invite_id:
        result["invite_id"] = invite_id
        result["signed_by"] = signed_by or invite_id  # Default: signed_by = invite_id
    return result


def make_invite_dep(
    event_id: str = "inv_123",
    user_id: str = "user_456",
    mode: str = "peer",
) -> dict:
    """Build invite dependency for testing."""
    return {
        "event_id": event_id,
        "event_data": {
            "type": "invite",
            "mode": mode,
            "user_id": user_id,
            "invite_pubkey": "invite_pubkey_123",
            "created_at": 999000,
        }
    }


def make_input(
    event_id: str = "ps_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_456",
    recorded_at: int = 1000001,
    signature_valid: bool = True,
    invite: dict | None = None,
) -> dict:
    """Build complete input dict for testing."""
    # Default: invite-signed mode
    if event_data is None:
        event_data = make_event_data(invite_id="inv_123", peer_id=recorded_by)

    deps = {}
    if invite is not None:
        deps["invite"] = invite
    elif event_data.get("invite_id"):
        # Auto-create invite dep for invite-signed mode
        deps["invite"] = make_invite_dep(event_id=event_data["invite_id"])

    return {
        "event_id": event_id,
        "event_data": event_data,
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "signature_valid": signature_valid,
        "dependencies": deps,
    }


# ============================================================================
# API FUNCTIONS
# ============================================================================

def create(peer_id: str, t_ms: int, db: Any,
           invite_id: str,
           invite_private_key: bytes) -> str:
    """Create a shareable peer_shared event from a local peer.

    peer_shared is ALWAYS signed by an invite (mode=peer). This ensures every
    peer_shared is linked to a user_id via the invite chain.

    Args:
        peer_id: Local peer ID
        t_ms: Timestamp
        db: Database connection
        invite_id: Peer invite ID (required - from invite(mode=peer))
        invite_private_key: Invite private key for signing (required)

    Returns:
        peer_shared_id: The ID of the created peer_shared event
    """
    log.info(f"peer_shared.create() creating peer_shared for peer_id={peer_id}, t_ms={t_ms}, invite_id={invite_id[:20]}...")

    # Get peer's public key (always needed)
    public_key = peer.get_public_key(peer_id, peer_id, db)

    # Create event dict
    event_data = {
        'type': 'peer_shared',
        'public_key': crypto.b64encode(public_key),
        'peer_id': peer_id,  # Link back to local peer
        'created_at': t_ms
    }

    # Sign with invite key (links peer_shared to user via invite)
    event_data['invite_id'] = invite_id
    event_data['signed_by'] = invite_id
    signed_event = crypto.sign_event(event_data, invite_private_key)
    log.info(f"peer_shared.create() signed with invite key (signed_by={invite_id[:20]}...)")

    # Canonicalize to get deterministic blob
    blob = crypto.canonicalize_json(signed_event)

    # Store event with recorded wrapper and projection
    peer_shared_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"peer_shared.create() created peer_shared_id={peer_shared_id}")
    return peer_shared_id


def project_event(peer_shared_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project peer_shared event into peers_shared table (including user_id if invite-based).

    Uses pure functional projector.
    Note: Keeps side effect for device linking (seeding group keys to new devices).
    """
    log.debug(f"peer_shared.project_event() projecting peer_shared_id={peer_shared_id[:20]}...")

    from projection import resolve

    safedb = create_safe_db(db, recorded_by=recorded_by)

    input_dict = resolve("peer_shared", peer_shared_id, recorded_by, recorded_at, db)
    if not input_dict:
        return None

    result = project(input_dict)

    if result.blocked or not result.valid:
        log.warning(f"peer_shared.project_event() failed: {result.reason}")
        return None

    # Apply peers_shared (INSERT OR IGNORE)
    for row in result.tables.get("peers_shared", []):
        safedb.execute(
            """INSERT OR IGNORE INTO peers_shared
               (peer_shared_id, peer_id, public_key, user_id, created_at, recorded_by, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (row["peer_shared_id"], row["peer_id"], row["public_key"], row["user_id"],
             row["created_at"], row["recorded_by"], row["recorded_at"])
        )

    # Apply peer_self (INSERT OR REPLACE - updates existing)
    for row in result.tables.get("peer_self", []):
        safedb.execute(
            "INSERT OR REPLACE INTO peer_self (peer_id, peer_shared_id, user_id, recorded_by, recorded_at) VALUES (?, ?, ?, ?, ?)",
            (row["peer_id"], row["peer_shared_id"], row["user_id"], row["recorded_by"], row["recorded_at"])
        )

    # Apply valid_events (INSERT OR IGNORE)
    for row in result.tables.get("valid_events", []):
        safedb.execute(
            "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
            (row["event_id"], row["recorded_by"])
        )

    # Extract user_id for side effect logic
    event_data = input_dict["event_data"]
    owner_peer_id = event_data["peer_id"]
    user_id = None
    for row in result.tables.get("peers_shared", []):
        user_id = row.get("user_id")
        break

    log.info(f"peer_shared.project() projected peer_shared_id={peer_shared_id[:20]}...")

    # Side effect: For invite-based peer_shared (device linking): Seed group keys for all groups this user belongs to
    # This ensures new devices get keys for existing groups created after the invite
    # Keys must be sealed to the SAME invite prekey that the device will use to decrypt them
    if user_id and owner_peer_id != recorded_by:
        # Only seed if this is a peer_shared from another device (not our own)
        log.info(f"peer_shared.project() seeding group keys for user {user_id[:20]}... new device {peer_shared_id[:20]}...")

        # Find the active mode='peer' invite for this user (device linking invite)
        invite_rows = safedb.query(
            """SELECT invite_id FROM invites
               WHERE mode = 'peer' AND user_id = ? AND recorded_by = ?
               ORDER BY created_at DESC LIMIT 1""",
            (user_id, recorded_by)
        )

        if not invite_rows:
            log.warning(f"peer_shared.project() no active device link invite found for user {user_id[:20]}..., skipping key seeding")
        else:
            invite_id = invite_rows[0]['invite_id']

            # Get our own peer_shared_id to sign the sealed keys
            our_peer_shared_id = None
            our_peer_row = safedb.query_one(
                "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
                (recorded_by, recorded_by)
            )
            if our_peer_row:
                our_peer_shared_id = our_peer_row['peer_shared_id']

            if not our_peer_shared_id:
                log.warning(f"peer_shared.project() couldn't find our peer_shared_id, skipping device link seeding")
            else:
                # Query all groups this user is a member of
                group_rows = safedb.query(
                    """SELECT DISTINCT g.group_id, g.key_id
                       FROM group_members gm
                       JOIN groups g ON gm.group_id = g.group_id AND gm.recorded_by = g.recorded_by
                       WHERE gm.user_id = ? AND gm.recorded_by = ?
                       ORDER BY g.group_id""",
                    (user_id, recorded_by)
                )

                log.info(f"peer_shared.project() found {len(group_rows)} groups for user {user_id[:20]}...")

                # For each group, create group_key_shared sealed to the invite prekey
                from events.group import group_key_shared
                key_share_ts = recorded_at + 1000  # Space out timestamps
                for group_row in group_rows:
                    group_id = group_row['group_id']
                    key_id = group_row['key_id']

                    try:
                        # Seal to the invite prekey (same mechanism as invite.create)
                        # This allows the new device to decrypt with invite_private_key
                        group_key_shared.create_for_invite(
                            key_id=key_id,
                            peer_id=recorded_by,  # Current peer creates the seal
                            peer_shared_id=our_peer_shared_id,  # Our peer_shared signs it
                            invite_id=invite_id,  # Seal to this invite's prekey
                            t_ms=key_share_ts,
                            db=db
                        )
                        log.info(f"peer_shared.project() sealed group key {key_id[:20]}... to invite prekey for new device {peer_shared_id[:20]}...")
                        key_share_ts += 1
                    except Exception as e:
                        log.warning(f"peer_shared.project() failed to seal group {group_id[:20]}... to invite prekey: {e}")

    return peer_shared_id


def get_public_key(peer_shared_id: str, recorded_by: str, db: Any) -> bytes:
    """Get public key for a peer_shared_id from the perspective of recorded_by."""
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one(
        "SELECT public_key FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ? LIMIT 1",
        (peer_shared_id, recorded_by)
    )
    if not row:
        raise ValueError(f"peer_shared not found: {peer_shared_id} for peer {recorded_by}")
    # public_key is stored as base64 string
    return crypto.b64decode(row['public_key'])


def get_peer_id_for_signing(peer_shared_id: str, recorded_by: str, db: Any) -> str:
    """Get the local peer_id associated with a peer_shared_id for signing.

    Args:
        peer_shared_id: The public peer_shared ID
        recorded_by: Peer ID requesting access (for access control)
        db: Database connection

    Returns:
        Local peer_id for signing

    Raises:
        ValueError: If peer_shared not found or peer doesn't have access
    """
    unsafedb = create_unsafe_db(db)

    # Get the event blob
    blob = store.get(peer_shared_id, unsafedb)
    if not blob:
        raise ValueError(f"peer_shared not found: {peer_shared_id}")

    event_data = crypto.parse_json(blob)
    peer_id = event_data.get('peer_id')
    if not peer_id:
        raise ValueError(f"peer_id not found in peer_shared event: {peer_shared_id}")

    # Security: Only allow access if the requester owns this peer_shared_id
    if peer_id != recorded_by:
        raise ValueError(f"access denied: peer {recorded_by} cannot access signing info for peer_shared {peer_shared_id}")

    return peer_id


def join(peer_id: str, peer_invite_id: str, peer_invite_private_key: bytes,
         user_id: str | None, prekey_id: str | None, t_ms: int, db: Any) -> dict[str, Any]:
    """Join/link a peer via peer invite (first peer or linking device).

    This is the canonical, reusable operation for peer joining/linking.
    Called by user.join() and user.new_network() to avoid code duplication.

    Args:
        peer_id: Local peer ID (must already exist - create with peer.create() first)
        peer_invite_id: Peer invite ID (mode='peer')
        peer_invite_private_key: Private key from peer invite URL
        user_id: User being linked to (extracted from invite)
        prekey_id: Optional group_prekey_shared_id from invite URL
        t_ms: Base timestamp
        db: Database connection

    Returns:
        {
            'peer_id': str,
            'peer_shared_id': str,
            'user_id': str,
            'prekey_id': str | None,
            'transit_prekey_id': str,
            'transit_prekey_shared_id': str,
        }
    """
    log.info(f"peer_shared.join() peer_id={peer_id}, peer_invite_id={peer_invite_id[:20]}..., user_id={user_id[:20] if user_id else 'None'}...")

    # 1. Create peer_shared signed by peer_invite (proves access to invite)
    peer_shared_id = create(
        peer_id=peer_id,
        t_ms=t_ms,
        db=db,
        invite_id=peer_invite_id,
        invite_private_key=peer_invite_private_key
    )
    log.info(f"peer_shared.join() created peer_shared: {peer_shared_id[:20]}...")

    # 2. Create invite_accepted to event-source the secrets (triggers notify_event_valid cascade)
    from events.identity import invite_accepted
    invite_accepted_id = invite_accepted.create(
        invite_id=peer_invite_id,
        invite_prekey_id=prekey_id,
        invite_private_key=peer_invite_private_key,
        peer_id=peer_id,
        t_ms=t_ms + 1,
        db=db
    )
    log.info(f"peer_shared.join() created invite_accepted: {invite_accepted_id[:20]}...")

    # 3. Auto-create transit_prekey + transit_prekey_shared for sync
    from events.network import transit_prekey, transit_prekey_shared
    transit_prekey_id, transit_prekey_private = transit_prekey.create(
        peer_id=peer_id,
        t_ms=t_ms + 2,
        db=db
    )

    transit_prekey_shared_id = transit_prekey_shared.create(
        prekey_id=transit_prekey_id,
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        t_ms=t_ms + 3,
        db=db
    )
    log.info(f"peer_shared.join() created transit prekey_shared: {transit_prekey_shared_id[:20]}...")

    # 4. Project peer_shared immediately (establishes peer↔user link, updates peer_self)
    from events.network import recorded
    peer_shared_recorded_id = recorded.create(peer_shared_id, peer_id, t_ms + 4, db, return_dupes=True)
    recorded.project_ids([peer_shared_recorded_id], db)
    log.info(f"peer_shared.join() projected peer_shared, peer_self updated with user_id")

    # invite_accepted.project() will be called by recorded.project_ids() which will:
    # - Store invite_private_key in group_prekeys (for GKS decryption)
    # - Call notify_event_valid(peer_invite_id) - unblocks dependent events
    # - Call notify_event_valid(prekey_id) - unblocks group_key_shared events sealed to this prekey
    # This is the "unblock cascade" that drives the joining flow

    return {
        'peer_id': peer_id,
        'peer_shared_id': peer_shared_id,
        'user_id': user_id,
        'prekey_id': prekey_id,
        'transit_prekey_id': transit_prekey_id,
        'transit_prekey_shared_id': transit_prekey_shared_id,
        'invite_accepted_id': invite_accepted_id,
    }
