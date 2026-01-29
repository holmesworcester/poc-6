"""Peer shared event type (shareable public identity)."""

# Registry metadata
EVENT_TYPE = 'peer_shared'
SHAREABLE = True  # Public identity syncs across network
PROJECTION_TABLE = ('peers_shared', 'peer_shared_id')

from typing import Any
import logging
from core import crypto
from core import store
from core import wire_format
from events.identity import peer
from core.db import create_safe_db, create_unsafe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)

# Wire format constants
WIRE_TYPE_CODE = 0x24  # TYPE_PEER_SHARED
WIRE_PLAINTEXT_SIZE = 344  # PEER_SHARED_PLAINTEXT_SIZE


# Wire format functions - encode/decode for peer_shared event type

def encode_plaintext(
    public_key: bytes,
    peer_id: bytes,
    invite_id: bytes | None,
) -> bytes:
    """Encode a peer_shared payload plaintext (pre-encryption).

    Layout (344 bytes):
    - public_key (32)
    - peer_id (16)
    - invite_id (16) - zero-padded if None
    - pad
    """
    wire_format._require_len("public_key", public_key, wire_format.PUBKEY_SIZE)
    wire_format._require_len("peer_id", peer_id, 16)
    invite_bytes = invite_id or (b"\x00" * 16)
    wire_format._require_len("invite_id", invite_bytes, 16)
    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:32] = public_key
    payload[32:48] = peer_id
    payload[48:64] = invite_bytes
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a peer_shared payload plaintext (post-decryption)."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(
            f"peer_shared plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    public_key = data[0:32]
    peer_id = data[32:48]
    invite_id = data[48:64]
    if invite_id == b"\x00" * 16:
        invite_id = None
    return {"public_key": public_key, "peer_id": peer_id, "invite_id": invite_id}


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a peer_shared wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(
    *,
    public_key_b64: str,
    peer_id_b64: str,
    invite_id_b64: str,
    created_at_ms: int,
    private_key: bytes,
) -> bytes:
    """Encode a complete peer_shared wire event."""
    public_key = crypto.b64decode(public_key_b64)
    peer_id = crypto.b64decode(peer_id_b64)
    invite_id = crypto.b64decode(invite_id_b64)
    plaintext = encode_plaintext(
        public_key=public_key,
        peer_id=peer_id,
        invite_id=invite_id,
    )
    header = wire_format.WireHeader(
        version=1,
        event_type=WIRE_TYPE_CODE,
        flags=0,
        signer_type=wire_format.SIGNER_INVITE,
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=wire_format._require_len("signer_id", invite_id, wire_format.SIGNER_ID_SIZE),
    )
    signed_bytes = wire_format._signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = wire_format._pad_payload(plaintext)
    return wire_format.build_envelope(header, payload, signature)


def decode_wire_event(data: bytes) -> dict[str, Any]:
    """Decode a peer_shared wire event."""
    header, payload, signature = wire_format.parse_envelope(data)
    if header.event_type != WIRE_TYPE_CODE:
        raise ValueError("unexpected event type for peer_shared")
    plaintext = payload[:WIRE_PLAINTEXT_SIZE]
    decoded = decode_plaintext(plaintext)
    event_data = {
        "type": "peer_shared",
        "public_key": crypto.b64encode(decoded["public_key"]),
        "peer_id": crypto.b64encode(decoded["peer_id"]),
        "created_at": header.created_at_ms,
    }
    if decoded["invite_id"]:
        invite_id_b64 = crypto.b64encode(decoded["invite_id"])
        event_data["invite_id"] = invite_id_b64
        event_data["signed_by"] = invite_id_b64
        event_data["signer_type"] = wire_format.signer_type_to_str(header.signer_type)
        event_data["_wire_signature"] = signature
        event_data["_wire_signed_bytes"] = wire_format._signing_bytes(header, plaintext)
    return event_data


# v2 event specification - peer_shared uses legacy NO_DEPS_TYPES behavior
# peer_shared has invite as optional dep with required_if_present:
# - If invite_id is in event data, block until invite is projected (to get user_id)
# - If no invite_id (bootstrap peer_shared), don't block
# The blocking ensures proper causal ordering - sync will eventually deliver the invite.
EVENT_SPEC = {
    'encrypted': False,
    'signer': None,  # Signer verification done by legacy projector
    'requires': {},
    'optional': {
        'invite': {
            'source': 'table',
            'table': 'invites',
            'key': 'invite_id',
            'key_from': 'invite_id',
            'fields': ['invite_id', 'invite_pubkey', 'user_id'],
            'required_if_present': True,  # Block until invite is projected (need user_id)
        },
    },
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for peer_shared events.

    peer_shared events represent public peer identity.
    Can be signed by invite (mode=peer) or self-signed (bootstrap).
    The resolver handles signature verification.

    Writes to: peers_shared, peer_self (if this is our own peer and invite-based)
    """
    event_data = ctx.event_data

    if event_data.get('type') != 'peer_shared':
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Extract fields
    public_key_b64 = event_data.get('public_key')
    if not public_key_b64:
        return ProjectorResult(writes=tuple(), valid_event=False)

    owner_peer_id = event_data.get('peer_id')
    if not owner_peer_id:
        return ProjectorResult(writes=tuple(), valid_event=False)

    created_at = event_data.get('created_at')

    # Determine verification mode and user_id
    invite_id = event_data.get('invite_id')
    signed_by = event_data.get('signed_by')
    user_id = None

    _wire_shadow_peer_shared(public_key_b64, owner_peer_id, invite_id)

    if invite_id and signed_by == invite_id:
        # Invite-based - get user_id from invite
        invite_row = ctx.deps.get('invite')
        if invite_row:
            user_id = invite_row.get('user_id')

    writes = [
        WriteOp(
            op='insert',
            table='peers_shared',
            values={
                'peer_shared_id': ctx.event_id,
                'peer_id': owner_peer_id,
                'public_key': public_key_b64,
                'user_id': user_id,  # NULL if self-signed bootstrap
                'device_name': None,  # device_name from encrypted peer_name_update events
                'created_at': created_at,
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
    ]

    # Update peer_self if this is our own peer AND invite-based (has user_id)
    # Self-signed peer_shared from bootstrap should NOT update peer_self
    if owner_peer_id == ctx.recorded_by and user_id:
        writes.append(
            WriteOp(
                op='insert',  # Will be INSERT OR REPLACE in apply
                table='peer_self',
                values={
                    'peer_id': owner_peer_id,
                    'peer_shared_id': ctx.event_id,
                    'user_id': user_id,
                    'recorded_by': ctx.recorded_by,
                    'recorded_at': ctx.recorded_at,
                },
            )
        )

    return ProjectorResult(writes=tuple(writes), valid_event=True)


def create(peer_id: str, t_ms: int, db: Any,
           invite_id: str,
           invite_private_key: bytes,
           device_name: str = "Device") -> str:
    """Create a shareable peer_shared event from a local peer.

    peer_shared is ALWAYS signed by an invite (mode=peer). This ensures every
    peer_shared is linked to a user_id via the invite chain.

    NOTE: The device_name parameter is accepted for API compatibility but NOT
    stored in the event. Device names are transmitted via encrypted peer_name_update
    events to protect privacy from NETWORK ACTIVE ATTACKER.

    Args:
        peer_id: Local peer ID
        t_ms: Timestamp
        db: Database connection
        invite_id: Peer invite ID (required - from invite(mode=peer))
        invite_private_key: Invite private key for signing (required)
        device_name: Device name - ignored here, use peer_name_update instead

    Returns:
        peer_shared_id: The ID of the created peer_shared event
    """
    log.info(f"peer_shared.create() creating peer_shared for peer_id={peer_id}, t_ms={t_ms}, invite_id={invite_id[:20]}..., device_name={device_name}")

    # Get peer's public key (always needed)
    public_key = peer.get_public_key(peer_id, peer_id, db)

    # Create event dict
    # NOTE: device_name is NOT stored - device names come from encrypted peer_name_update events
    event_data = {
        'type': 'peer_shared',
        'public_key': crypto.b64encode(public_key),
        'peer_id': peer_id,  # Link back to local peer
        'invite_id': invite_id,  # Link to invite that authorized this peer
        'signed_by': invite_id,  # Polymorphic signer field
        'signer_type': 'invite',  # v2: peer_shared events are signed by invite
        'created_at': t_ms
    }

    _wire_shadow_peer_shared(event_data['public_key'], peer_id, invite_id)

    blob = encode_wire_event(
        public_key_b64=event_data['public_key'],
        peer_id_b64=peer_id,
        invite_id_b64=invite_id,
        created_at_ms=t_ms,
        private_key=invite_private_key,
    )
    peer_shared_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"peer_shared.create() created peer_shared_id={peer_shared_id}")
    return peer_shared_id


def _wire_shadow_peer_shared(public_key_b64: str, peer_id: str, invite_id: str | None) -> None:
    """Validate peer_shared fields against the fixed-size wire payload layout."""
    plaintext = encode_plaintext(
        public_key=crypto.b64decode(public_key_b64),
        peer_id=crypto.b64decode(peer_id),
        invite_id=crypto.b64decode(invite_id) if invite_id else None,
    )
    decoded = decode_plaintext(plaintext)
    if decoded["peer_id"] != crypto.b64decode(peer_id):
        raise ValueError("wire shadow decode peer_id mismatch")


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


def get_public_key_from_store(peer_shared_id: str, db: Any) -> bytes | None:
    """Get public key by reading directly from the peer_shared blob in store.

    This is the conventional approach for projectors that need a peer's public key
    during cascade processing - reads from the raw blob rather than projection tables.
    This avoids timing issues where an event is in valid_events but not yet projected.

    Args:
        peer_shared_id: The peer_shared event ID
        db: Database connection

    Returns:
        Public key bytes, or None if blob not found or not a peer_shared event
    """
    unsafedb = create_unsafe_db(db)
    blob = store.get(peer_shared_id, unsafedb)
    if not blob:
        log.warning(f"get_public_key_from_store() blob not found: {peer_shared_id[:20]}...")
        return None

    try:
        if not is_wire_envelope(blob):
            log.warning(f"get_public_key_from_store() expected wire peer_shared event: {peer_shared_id[:20]}...")
            return None
        event_data = decode_wire_event(blob)
        if event_data.get('type') != 'peer_shared':
            log.warning(f"get_public_key_from_store() not a peer_shared event: {peer_shared_id[:20]}...")
            return None
        return crypto.b64decode(event_data['public_key'])
    except Exception as e:
        log.warning(f"get_public_key_from_store() failed to parse blob: {peer_shared_id[:20]}... {e}")
        return None


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

    if not is_wire_envelope(blob):
        raise ValueError(f"peer_shared event not in wire format: {peer_shared_id}")
    event_data = decode_wire_event(blob)
    peer_id = event_data.get('peer_id')
    if not peer_id:
        raise ValueError(f"peer_id not found in peer_shared event: {peer_shared_id}")

    # Security: Only allow access if the requester owns this peer_shared_id
    if peer_id != recorded_by:
        raise ValueError(f"access denied: peer {recorded_by} cannot access signing info for peer_shared {peer_shared_id}")

    return peer_id


def get_for_user(user_id: str, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get the first peer_shared for a user_id.

    Args:
        user_id: User ID to look up
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        Dict with peer_shared_id, or None if not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    return safedb.query_one(
        "SELECT peer_shared_id FROM peers_shared WHERE user_id = ? AND recorded_by = ? LIMIT 1",
        (user_id, recorded_by)
    )


def get_user_id(peer_shared_id: str, recorded_by: str, db: Any) -> str | None:
    """Get the user_id associated with a peer_shared_id.

    Args:
        peer_shared_id: Public peer ID to look up
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        user_id string if found, None otherwise
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one(
        "SELECT user_id FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ? LIMIT 1",
        (peer_shared_id, recorded_by)
    )
    return row['user_id'] if row and row['user_id'] else None


def get_self(peer_id: str, db: Any) -> dict[str, str] | None:
    """Get the identity (peer_shared_id and user_id) for a local peer.

    Looks up the peer_self table to get the canonical mapping from local peer
    to its public peer_shared identity and associated user.

    Args:
        peer_id: Local peer ID
        db: Database connection

    Returns:
        Dict with 'peer_shared_id' and 'user_id' if found, None otherwise
    """
    safedb = create_safe_db(db, recorded_by=peer_id)
    row = safedb.query_one(
        "SELECT peer_shared_id, user_id FROM peer_self WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (peer_id, peer_id)
    )
    if not row:
        return None
    return {'peer_shared_id': row['peer_shared_id'], 'user_id': row['user_id']}


def get_device_name(peer_shared_id: str, recorded_by: str, db: Any) -> str:
    """Get device name for a peer_shared_id.

    Prefers encrypted name from peer_names table (from peer_name_update events),
    falls back to peers_shared.device_name (legacy), then to "Device" default.

    Args:
        peer_shared_id: The public peer_shared ID
        recorded_by: Peer ID requesting access (for access control)
        db: Database connection

    Returns:
        Device name (e.g., "Phone", "Desktop") or "Device" if not set
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # First try encrypted peer_names table (preferred)
    peer_name_row = safedb.query_one(
        "SELECT name FROM peer_names WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (peer_shared_id, recorded_by)
    )
    if peer_name_row and peer_name_row['name']:
        return peer_name_row['name']

    # Fall back to peers_shared.device_name (legacy)
    row = safedb.query_one(
        "SELECT device_name FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ? LIMIT 1",
        (peer_shared_id, recorded_by)
    )
    return row['device_name'] if row and row['device_name'] else "Device"


def get_for_peer(peer_id: str, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get peer_shared info for a local peer.

    Combines get_self (peer_shared_id, user_id) with device_name lookup.

    Args:
        peer_id: Local peer ID
        recorded_by: Peer ID requesting access
        db: Database connection

    Returns:
        Dict with peer_shared_id, user_id, device_name if found, None otherwise
    """
    self_info = get_self(peer_id, db)
    if not self_info:
        return None

    peer_shared_id = self_info['peer_shared_id']
    device_name = get_device_name(peer_shared_id, recorded_by, db)

    return {
        'peer_shared_id': peer_shared_id,
        'user_id': self_info.get('user_id'),
        'device_name': device_name
    }


def join(peer_id: str, peer_invite_id: str, peer_invite_private_key: bytes,
         user_id: str | None, prekey_id: str | None, t_ms: int, db: Any,
         device_name: str = "Device", network_id: str | None = None) -> dict[str, Any]:
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
        device_name: Device name (e.g., "Phone", "Desktop")
        network_id: Network ID for trust anchoring (required for proper cascade)

    Returns:
        {
            'peer_id': str,
            'peer_shared_id': str,
            'user_id': str,
            'prekey_id': str | None,
            'connection_prekey_id': str,
            'connection_prekey_shared_id': str,
        }
    """
    log.info(f"peer_shared.join() peer_id={peer_id}, peer_invite_id={peer_invite_id[:20]}..., user_id={user_id[:20] if user_id else 'None'}..., device_name={device_name}")

    # 0. Store invite prekey for decrypting group_key_shared events
    # For device linking, GKS events are sealed to the invite prekey.
    # Since group_prekey blobs are deterministic (same key material = same hash),
    # this produces the SAME prekey_id that the inviting device created.
    if prekey_id:
        from nacl.signing import SigningKey
        from events.group import group_prekey
        signing_key = SigningKey(peer_invite_private_key)
        invite_pubkey = bytes(signing_key.verify_key)
        created_prekey_id = group_prekey.create_from_material(
            public_key=invite_pubkey,
            private_key=peer_invite_private_key,
            peer_id=peer_id,
            t_ms=t_ms,
            db=db
        )
        log.info(f"peer_shared.join() stored invite prekey: {created_prekey_id[:20]}... (expected: {prekey_id[:20]}...)")

    # 1. Create invite_accepted FIRST - stores invite_private_key in invite_accepteds
    # This MUST happen before peer_shared.create() so peer_shared.project() can
    # derive invite_pubkey from the stored private key (distributed bootstrap case)
    from events.identity import invite_accepted
    peer_invite_link_data = {
        'invite_id': peer_invite_id,
        'invite_prekey_id': prekey_id,
        'invite_private_key': crypto.b64encode(peer_invite_private_key),
        'user_id': user_id,  # User being linked to (for device linking)
        'network_id': network_id,  # Required for trust anchoring (network is ALWAYS the trust anchor)
    }
    invite_accepted_id = invite_accepted.create(
        invite_link_data=peer_invite_link_data,
        peer_id=peer_id,
        t_ms=t_ms,  # No offset needed - DAG deps handle ordering
        db=db
    )
    log.info(f"peer_shared.join() created invite_accepted: {invite_accepted_id[:20]}...")

    # 2. NOW create peer_shared - projection can derive invite_pubkey from invite_accepteds
    peer_shared_id = create(
        peer_id=peer_id,
        t_ms=t_ms,
        db=db,
        invite_id=peer_invite_id,
        invite_private_key=peer_invite_private_key,
        device_name=device_name
    )
    log.info(f"peer_shared.join() created peer_shared: {peer_shared_id[:20]}...")

    # 3. Auto-create transit_prekey + transit_prekey_shared for sync
    from events.network import connection_prekey, connection_prekey_shared
    connection_prekey_id, transit_prekey_private = connection_prekey.create(
        peer_id=peer_id,
        t_ms=t_ms,  # No offset needed - DAG deps handle ordering
        db=db
    )

    connection_prekey_shared_id = connection_prekey_shared.create(
        prekey_id=connection_prekey_id,
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        t_ms=t_ms,  # No offset needed - DAG deps handle ordering
        db=db
    )
    log.info(f"peer_shared.join() created transit prekey_shared: {connection_prekey_shared_id[:20]}...")

    # 4. Project peer_shared immediately (establishes peer↔user link, updates peer_self)
    from core import recorded
    peer_shared_recorded_id = recorded.create(peer_shared_id, peer_id, t_ms, db, return_dupes=True)
    recorded.project_ids([peer_shared_recorded_id], db)
    log.info(f"peer_shared.join() projected peer_shared, peer_self updated with user_id")

    # invite_accepted.project() will be called by recorded.project_ids() which will:
    # - Store invite_private_key in group_prekeys (for GKS decryption)
    # - Call notify_event_valid(peer_invite_id) - unblocks dependent events
    # - Call notify_event_valid(prekey_id) - unblocks group_key_shared events sealed to this prekey
    # This is the "unblock cascade" that drives the joining flow

    # Try to create peer_name_update event for device name (encrypted)
    # May fail if key not available yet - will be stored in pending_name_updates
    from events.identity import peer_name_update
    from core.db import create_safe_db
    safedb = create_safe_db(db, recorded_by=peer_id)
    try:
        peer_name_update_id = peer_name_update.create(
            peer_target_id=peer_shared_id,
            name=device_name,
            peer_id=peer_id,
            peer_shared_id=peer_shared_id,
            t_ms=t_ms,  # No offset needed - DAG deps handle ordering
            db=db
        )
        log.info(f"peer_shared.join() created peer_name_update: {peer_name_update_id[:20]}...")
    except peer_name_update.KeyNotAvailableError:
        # Key not available yet - store for later creation when group_key_shared arrives
        log.info(f"peer_shared.join() key not available yet, storing device name intent in pending_name_updates")
        import hashlib
        pending_id = hashlib.sha256(f"{peer_shared_id}:peer_name:{t_ms}".encode()).hexdigest()[:20]
        safedb.execute(
            """INSERT OR IGNORE INTO pending_name_updates
               (id, type, entity_id, name, peer_id, peer_shared_id, status, created_at, recorded_by, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pending_id, 'peer_name', peer_shared_id, device_name, peer_id, peer_shared_id,
             'waiting_for_key', t_ms, peer_id, t_ms)
        )
        log.info(f"peer_shared.join() stored pending device name for peer {peer_shared_id[:20]}...")

    return {
        'peer_id': peer_id,
        'peer_shared_id': peer_shared_id,
        'user_id': user_id,
        'prekey_id': prekey_id,
        'connection_prekey_id': connection_prekey_id,
        'connection_prekey_shared_id': connection_prekey_shared_id,
        'invite_accepted_id': invite_accepted_id,
    }
