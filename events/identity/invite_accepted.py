"""Invite accepted event type (local-only, captures invite acceptance)."""

# Registry metadata
EVENT_TYPE = 'invite_accepted'
SHAREABLE = False  # Local-only - captures out-of-band invite data
PROJECTION_TABLE = None

# Wire format constants
WIRE_TYPE_CODE = 0x2B  # TYPE_INVITE_ACCEPTED
WIRE_PLAINTEXT_SIZE = 344  # INVITE_ACCEPTED_PLAINTEXT_SIZE

from typing import Any
import base64
import logging
import struct
from core import crypto
from core import store
from core import wire_format
from events.identity import network as network_mod
from core.db import create_safe_db, create_unsafe_db
from core.projection.types import ProjectorResult, WriteOp, Command
from core.projection.apply import register_command_handler

log = logging.getLogger(__name__)


# Wire format functions - encode/decode for invite_accepted event type

def encode_plaintext(
    invite_id: bytes,
    invite_prekey_id: bytes | None,
    invite_private_key: bytes,
    inviter_peer_shared_id: bytes | None,
    network_id: bytes | None,
    channel_id: bytes | None,
    key_id: bytes | None,
    inviter_connection_prekey_public_key: bytes | None,
    inviter_connection_prekey_shared_id: bytes | None,
    inviter_connection_prekey_id: bytes | None,
    inviter_ip: str | None,
    inviter_port: int | None,
    link_user_id: bytes | None,
    inviter_peer_shared_blob_id: bytes | None,
) -> bytes:
    """Encode an invite_accepted payload plaintext (pre-encryption)."""
    wire_format._require_len("invite_id", invite_id, 16)
    wire_format._require_len("invite_private_key", invite_private_key, wire_format.PRIVKEY_SIZE)
    invite_prekey_bytes = invite_prekey_id or (b"\x00" * 16)
    wire_format._require_len("invite_prekey_id", invite_prekey_bytes, 16)
    inviter_peer_bytes = inviter_peer_shared_id or (b"\x00" * 16)
    wire_format._require_len("inviter_peer_shared_id", inviter_peer_bytes, 16)
    network_bytes = network_id or (b"\x00" * 16)
    wire_format._require_len("network_id", network_bytes, 16)
    channel_bytes = channel_id or (b"\x00" * 16)
    wire_format._require_len("channel_id", channel_bytes, 16)
    key_bytes = key_id or (b"\x00" * 16)
    wire_format._require_len("key_id", key_bytes, 16)
    inviter_prekey_pub = inviter_connection_prekey_public_key or (b"\x00" * wire_format.PUBKEY_SIZE)
    wire_format._require_len("inviter_connection_prekey_public_key", inviter_prekey_pub, wire_format.PUBKEY_SIZE)
    inviter_prekey_shared = inviter_connection_prekey_shared_id or (b"\x00" * 16)
    wire_format._require_len("inviter_connection_prekey_shared_id", inviter_prekey_shared, 16)
    inviter_prekey_id_bytes = inviter_connection_prekey_id or (b"\x00" * 16)
    wire_format._require_len("inviter_connection_prekey_id", inviter_prekey_id_bytes, 16)
    link_user_bytes = link_user_id or (b"\x00" * 16)
    wire_format._require_len("link_user_id", link_user_bytes, 16)
    blob_id_bytes = inviter_peer_shared_blob_id or (b"\x00" * 16)
    wire_format._require_len("inviter_peer_shared_blob_id", blob_id_bytes, 16)
    if inviter_port is not None and (inviter_port < 0 or inviter_port > 0xFFFF):
        raise ValueError("inviter_port must fit in u16")

    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:16] = invite_id
    payload[16:32] = invite_prekey_bytes
    payload[32:64] = invite_private_key
    payload[64:80] = inviter_peer_bytes
    payload[80:96] = network_bytes
    payload[96:112] = channel_bytes
    payload[112:128] = key_bytes
    payload[128:160] = inviter_prekey_pub
    payload[160:176] = inviter_prekey_shared
    payload[176:192] = inviter_prekey_id_bytes
    payload[192:208] = wire_format._encode_ip16(inviter_ip)
    struct.pack_into("<H", payload, 208, inviter_port or 0)
    payload[210:226] = link_user_bytes
    payload[226:242] = blob_id_bytes
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode an invite_accepted payload plaintext (post-decryption)."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(
            f"invite_accepted plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    invite_id = data[0:16]
    invite_prekey_id = data[16:32]
    invite_private_key = data[32:64]
    inviter_peer_shared_id = data[64:80]
    network_id = data[80:96]
    channel_id = data[96:112]
    key_id = data[112:128]
    inviter_connection_prekey_public_key = data[128:160]
    inviter_connection_prekey_shared_id = data[160:176]
    inviter_connection_prekey_id = data[176:192]
    inviter_ip = wire_format._decode_ip16(data[192:208])
    (inviter_port,) = struct.unpack_from("<H", data, 208)
    link_user_id = data[210:226]
    inviter_peer_shared_blob_id = data[226:242]
    if invite_prekey_id == b"\x00" * 16:
        invite_prekey_id = None
    if inviter_peer_shared_id == b"\x00" * 16:
        inviter_peer_shared_id = None
    if network_id == b"\x00" * 16:
        network_id = None
    if channel_id == b"\x00" * 16:
        channel_id = None
    if key_id == b"\x00" * 16:
        key_id = None
    if inviter_connection_prekey_public_key == b"\x00" * wire_format.PUBKEY_SIZE:
        inviter_connection_prekey_public_key = None
    if inviter_connection_prekey_shared_id == b"\x00" * 16:
        inviter_connection_prekey_shared_id = None
    if inviter_connection_prekey_id == b"\x00" * 16:
        inviter_connection_prekey_id = None
    if inviter_port == 0:
        inviter_port = None
    if link_user_id == b"\x00" * 16:
        link_user_id = None
    if inviter_peer_shared_blob_id == b"\x00" * 16:
        inviter_peer_shared_blob_id = None
    return {
        "invite_id": invite_id,
        "invite_prekey_id": invite_prekey_id,
        "invite_private_key": invite_private_key,
        "inviter_peer_shared_id": inviter_peer_shared_id,
        "network_id": network_id,
        "channel_id": channel_id,
        "key_id": key_id,
        "inviter_connection_prekey_public_key": inviter_connection_prekey_public_key,
        "inviter_connection_prekey_shared_id": inviter_connection_prekey_shared_id,
        "inviter_connection_prekey_id": inviter_connection_prekey_id,
        "inviter_ip": inviter_ip,
        "inviter_port": inviter_port,
        "link_user_id": link_user_id,
        "inviter_peer_shared_blob_id": inviter_peer_shared_blob_id,
    }


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is an invite_accepted wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(
    *,
    invite_id_b64: str,
    invite_prekey_id_b64: str | None,
    invite_private_key: bytes,
    inviter_peer_shared_id_b64: str | None,
    network_id_b64: str | None,
    channel_id_b64: str | None,
    key_id_b64: str | None,
    inviter_connection_prekey_public_key_b64: str | None,
    inviter_connection_prekey_shared_id_b64: str | None,
    inviter_connection_prekey_id_b64: str | None,
    inviter_ip: str | None,
    inviter_port: int | None,
    link_user_id_b64: str | None,
    inviter_peer_shared_blob_id_b64: str | None,
    created_at_ms: int,
    signed_by_b64: str | None,
) -> bytes:
    """Encode a complete invite_accepted wire event."""
    invite_id = crypto.b64decode(invite_id_b64)
    invite_prekey_id = crypto.b64decode(invite_prekey_id_b64) if invite_prekey_id_b64 else None
    inviter_peer_shared_id = (
        crypto.b64decode(inviter_peer_shared_id_b64) if inviter_peer_shared_id_b64 else None
    )
    network_id = crypto.b64decode(network_id_b64) if network_id_b64 else None
    channel_id = crypto.b64decode(channel_id_b64) if channel_id_b64 else None
    key_id = crypto.b64decode(key_id_b64) if key_id_b64 else None
    inviter_connection_prekey_public_key = (
        crypto.b64decode(inviter_connection_prekey_public_key_b64)
        if inviter_connection_prekey_public_key_b64
        else None
    )
    inviter_connection_prekey_shared_id = (
        crypto.b64decode(inviter_connection_prekey_shared_id_b64)
        if inviter_connection_prekey_shared_id_b64
        else None
    )
    inviter_connection_prekey_id = (
        crypto.b64decode(inviter_connection_prekey_id_b64) if inviter_connection_prekey_id_b64 else None
    )
    link_user_id = crypto.b64decode(link_user_id_b64) if link_user_id_b64 else None
    inviter_peer_shared_blob_id = (
        crypto.b64decode(inviter_peer_shared_blob_id_b64) if inviter_peer_shared_blob_id_b64 else None
    )
    signer_id = crypto.b64decode(signed_by_b64) if signed_by_b64 else (b"\x00" * wire_format.SIGNER_ID_SIZE)

    plaintext = encode_plaintext(
        invite_id=invite_id,
        invite_prekey_id=invite_prekey_id,
        invite_private_key=invite_private_key,
        inviter_peer_shared_id=inviter_peer_shared_id,
        network_id=network_id,
        channel_id=channel_id,
        key_id=key_id,
        inviter_connection_prekey_public_key=inviter_connection_prekey_public_key,
        inviter_connection_prekey_shared_id=inviter_connection_prekey_shared_id,
        inviter_connection_prekey_id=inviter_connection_prekey_id,
        inviter_ip=inviter_ip,
        inviter_port=inviter_port,
        link_user_id=link_user_id,
        inviter_peer_shared_blob_id=inviter_peer_shared_blob_id,
    )
    header = wire_format.WireHeader(
        version=1,
        event_type=WIRE_TYPE_CODE,
        flags=wire_format.FLAG_UNSIGNED,
        signer_type=wire_format.SIGNER_NONE,
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=wire_format._require_len("signer_id", signer_id, wire_format.SIGNER_ID_SIZE),
    )
    payload = wire_format._pad_payload(plaintext)
    signature = b"\x00" * wire_format.SIGNATURE_SIZE
    return wire_format.build_envelope(header, payload, signature)


def decode_wire_event(data: bytes) -> dict[str, Any]:
    """Decode an invite_accepted wire event."""
    header, payload, _signature = wire_format.parse_envelope(data)
    if header.event_type != WIRE_TYPE_CODE:
        raise ValueError("unexpected event type for invite_accepted")
    plaintext = payload[:WIRE_PLAINTEXT_SIZE]
    decoded = decode_plaintext(plaintext)
    invite_link_data: dict[str, Any] = {
        "invite_id": crypto.b64encode(decoded["invite_id"]),
        "invite_private_key": crypto.b64encode(decoded["invite_private_key"]),
    }
    if decoded["invite_prekey_id"]:
        invite_link_data["invite_prekey_id"] = crypto.b64encode(decoded["invite_prekey_id"])
    if decoded["inviter_peer_shared_id"]:
        invite_link_data["inviter_peer_shared_id"] = crypto.b64encode(decoded["inviter_peer_shared_id"])
    if decoded["network_id"]:
        invite_link_data["network_id"] = crypto.b64encode(decoded["network_id"])
    if decoded["channel_id"]:
        invite_link_data["channel_id"] = crypto.b64encode(decoded["channel_id"])
    if decoded["key_id"]:
        invite_link_data["key_id"] = crypto.b64encode(decoded["key_id"])
    if decoded["inviter_connection_prekey_public_key"]:
        invite_link_data["inviter_connection_prekey_public_key"] = crypto.b64encode(
            decoded["inviter_connection_prekey_public_key"]
        )
    if decoded["inviter_connection_prekey_shared_id"]:
        invite_link_data["inviter_connection_prekey_shared_id"] = crypto.b64encode(
            decoded["inviter_connection_prekey_shared_id"]
        )
    if decoded["inviter_connection_prekey_id"]:
        invite_link_data["inviter_connection_prekey_id"] = crypto.b64encode(
            decoded["inviter_connection_prekey_id"]
        )
    if decoded["inviter_ip"] is not None:
        invite_link_data["ip"] = decoded["inviter_ip"]
    if decoded["inviter_port"] is not None:
        invite_link_data["port"] = decoded["inviter_port"]
    if decoded["link_user_id"]:
        invite_link_data["user_id"] = crypto.b64encode(decoded["link_user_id"])
    if decoded["inviter_peer_shared_blob_id"]:
        invite_link_data["inviter_peer_shared_blob_id"] = crypto.b64encode(
            decoded["inviter_peer_shared_blob_id"]
        )

    event_data = {
        "type": EVENT_TYPE,
        "invite_link_data": invite_link_data,
        "created_at": header.created_at_ms,
    }
    if header.signer_id != b"\x00" * wire_format.SIGNER_ID_SIZE:
        event_data["signed_by"] = crypto.b64encode(header.signer_id)
    return event_data


# v2 event specification - local-only, unsigned (captures out-of-band invite data)
EVENT_SPEC = {
    'encrypted': False,
    'signer': None,  # Local-only, unsigned
    'requires': {},
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for invite_accepted events.

    invite_accepted is local-only and stores connection metadata from invite links.
    The engine handles marking network_id as valid (trust anchor).

    Writes to: invite_accepteds
    """
    event_data = ctx.event_data

    if event_data.get('type') != 'invite_accepted':
        return ProjectorResult(writes=tuple(), valid_event=False)

    invite_link_data = event_data.get('invite_link_data')
    if not invite_link_data:
        return ProjectorResult(writes=tuple(), valid_event=False)

    invite_id = invite_link_data.get('invite_id')
    if not invite_id:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Extract fields from invite_link_data
    inviter_peer_shared_id = invite_link_data.get('inviter_peer_shared_id')
    address = invite_link_data.get('ip')
    port = invite_link_data.get('port')
    network_id = invite_link_data.get('network_id')
    link_user_id = invite_link_data.get('user_id')
    inviter_connection_prekey_id = invite_link_data.get('inviter_connection_prekey_id')

    # Decode invite_private_key if present
    invite_private_key_b64 = invite_link_data.get('invite_private_key')
    invite_private_key = crypto.b64decode(invite_private_key_b64) if invite_private_key_b64 else None

    # Derive invite_pubkey from invite_private_key
    invite_pubkey_b64 = None
    if invite_private_key:
        from nacl.signing import SigningKey
        signing_key = SigningKey(invite_private_key)
        invite_pubkey_b64 = crypto.b64encode(bytes(signing_key.verify_key))

    # Decode inviter transit prekey if present
    inviter_connection_prekey_public_key = None
    if invite_link_data.get('inviter_connection_prekey_public_key'):
        inviter_connection_prekey_public_key = crypto.b64decode(invite_link_data['inviter_connection_prekey_public_key'])

    writes = [
        WriteOp(
            op='insert',
            table='invite_accepteds',
            values={
                'invite_id': invite_id,
                'inviter_peer_shared_id': inviter_peer_shared_id,
                'address': address,
                'port': port,
                'network_id': network_id,
                'user_id': link_user_id,
                'inviter_connection_prekey_id': inviter_connection_prekey_id,
                'inviter_connection_prekey_public_key': inviter_connection_prekey_public_key,
                'invite_private_key': invite_private_key,
                'invite_pubkey': invite_pubkey_b64,
                'created_at': event_data.get('created_at'),
                'recorded_by': ctx.recorded_by,
            },
        ),
    ]

    # Add trust anchor for network if present (device-wide table)
    if network_id:
        writes.append(WriteOp(
            op='insert',
            table='trust_anchors',
            values={
                'network_id': network_id,
                'recorded_by': ctx.recorded_by,
                'created_at': ctx.recorded_at,
            },
        ))

    # Command to handle network bootstrap (project network if in store, store inviter blob)
    commands = ()
    inviter_peer_shared_blob_b64 = invite_link_data.get('inviter_peer_shared_blob')
    inviter_peer_shared_blob_id = invite_link_data.get('inviter_peer_shared_blob_id')
    if network_id or inviter_peer_shared_blob_b64 or inviter_peer_shared_blob_id:
        commands = (
            Command(
                command_type='handle_invite_accepted_bootstrap',
                args={
                    'network_id': network_id,
                    'inviter_peer_shared_id': inviter_peer_shared_id,
                    'inviter_peer_shared_blob_b64': inviter_peer_shared_blob_b64,
                    'inviter_peer_shared_blob_id': inviter_peer_shared_blob_id,
                }
            ),
        )

    return ProjectorResult(writes=tuple(writes), valid_event=True, commands=commands)


def create(invite_link_data: dict, peer_id: str, t_ms: int, db: Any) -> str:
    """Create local invite_accepted event (not shareable).

    This event captures the invite acceptance action and stores
    out-of-band data from the invite link for event-sourcing (reprojection).

    By storing the invite_link_data, we ensure that:
    - Full reprojection works without the original invite link
    - The projection system has all necessary data to restore state
    - The trust anchor (network_id) can be marked valid naturally

    Args:
        invite_link_data: Invite link data dictionary containing:
            - invite_id: ID of the invite event (syncs separately)
            - invite_private_key: For prekey and signing
            - invite_prekey_id: Crypto hint for prekey ID
            - network_id: Network being joined (trust anchor)
            - inviter_peer_shared_id: Inviter's peer_shared_id
            - inviter_peer_shared_blob: Inviter's peer_shared blob (base64 urlsafe)
            - ip/port: Inviter's address for connection
        peer_id: Bob's peer_id (local)
        t_ms: Timestamp
        db: Database connection

    Returns:
        invite_accepted_id: Event ID
    """
    log.info(f"invite_accepted.create() for invite={invite_link_data['invite_id']}, peer={peer_id}")

    invite_id = invite_link_data.get('invite_id')
    invite_private_key_b64 = invite_link_data.get('invite_private_key')
    if not invite_id or not invite_private_key_b64:
        raise ValueError("invite_id and invite_private_key required for wire invite_accepted")

    invite_prekey_id = invite_link_data.get('invite_prekey_id')
    inviter_peer_shared_id = invite_link_data.get('inviter_peer_shared_id')
    network_id = invite_link_data.get('network_id')
    channel_id = invite_link_data.get('channel_id')
    key_id = invite_link_data.get('key_id')
    inviter_connection_prekey_public_key = invite_link_data.get('inviter_connection_prekey_public_key')
    inviter_connection_prekey_shared_id = invite_link_data.get('inviter_connection_prekey_shared_id')
    inviter_connection_prekey_id = invite_link_data.get('inviter_connection_prekey_id')
    inviter_ip = invite_link_data.get('ip')
    inviter_port = invite_link_data.get('port')
    link_user_id = invite_link_data.get('user_id')

    inviter_peer_shared_blob_id = invite_link_data.get('inviter_peer_shared_blob_id')
    inviter_peer_shared_blob_b64 = invite_link_data.get('inviter_peer_shared_blob')
    if not inviter_peer_shared_blob_id and inviter_peer_shared_blob_b64:
        padding = 4 - len(inviter_peer_shared_blob_b64) % 4
        if padding != 4:
            inviter_peer_shared_blob_b64 += '=' * padding
        inviter_peer_shared_blob = base64.urlsafe_b64decode(inviter_peer_shared_blob_b64)
        unsafedb = create_unsafe_db(db)
        inviter_peer_shared_blob_id = store.blob(inviter_peer_shared_blob, t_ms, True, unsafedb)

    blob = encode_wire_event(
        invite_id_b64=invite_id,
        invite_prekey_id_b64=invite_prekey_id,
        invite_private_key=crypto.b64decode(invite_private_key_b64),
        inviter_peer_shared_id_b64=inviter_peer_shared_id,
        network_id_b64=network_id,
        channel_id_b64=channel_id,
        key_id_b64=key_id,
        inviter_connection_prekey_public_key_b64=inviter_connection_prekey_public_key,
        inviter_connection_prekey_shared_id_b64=inviter_connection_prekey_shared_id,
        inviter_connection_prekey_id_b64=inviter_connection_prekey_id,
        inviter_ip=inviter_ip,
        inviter_port=inviter_port,
        link_user_id_b64=link_user_id,
        inviter_peer_shared_blob_id_b64=inviter_peer_shared_blob_id,
        created_at_ms=t_ms,
        signed_by_b64=peer_id,
    )
    invite_accepted_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"invite_accepted.create() created invite_accepted_id={invite_accepted_id}")
    return invite_accepted_id


def _handle_invite_accepted_bootstrap(args: dict, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Command handler for invite_accepted bootstrap side effects.

    1. Project network if blob is in store (trust anchor already set by write)
    2. Store inviter's peer_shared blob from link data
    """
    from core import recorded as recorded_module
    from events.identity import network as network_module
    from events import registry
    from core.projection import resolver as projection_resolver
    from core.projection import apply as projection_apply
    from core import queues

    safedb = create_safe_db(db, recorded_by=recorded_by)
    unsafedb = create_unsafe_db(db)

    network_id = args.get('network_id')
    inviter_peer_shared_id = args.get('inviter_peer_shared_id')
    inviter_peer_shared_blob_b64 = args.get('inviter_peer_shared_blob_b64')
    inviter_peer_shared_blob_id = args.get('inviter_peer_shared_blob_id')

    # Project network if blob is in store and not already projected
    if network_id:
        network_blob = store.get(network_id, unsafedb)
        if network_blob:
            already_projected = safedb.query_one(
                "SELECT 1 FROM networks WHERE network_id = ? AND recorded_by = ?",
                (network_id, recorded_by)
            )
            if not already_projected:
                # Use v2 projection path instead of legacy project()
                try:
                    if not network_mod.is_wire_envelope(network_blob):
                        log.warning(f"[INVITE_ACCEPTED_BOOTSTRAP] expected wire network event: {network_id[:20]}...")
                        network_data = None
                    else:
                        network_data = network_mod.decode_wire_event(network_blob)
                except Exception as e:
                    log.warning(f"[INVITE_ACCEPTED_BOOTSTRAP] failed to parse network blob: {e}")
                    network_data = None

                if network_data:
                    resolve_result = projection_resolver.resolve_event(
                        ref_id=network_id,
                        event_type='network',
                        event_data=network_data,
                        recorded_by=recorded_by,
                        recorded_at=recorded_at,
                        db=db,
                    )

                    if resolve_result.status == 'ok' and resolve_result.ctx:
                        project_pure_fn = registry.get_project_pure_fn('network')
                        if project_pure_fn:
                            projector_result = project_pure_fn(resolve_result.ctx)
                            projection_apply.apply_writes(projector_result, recorded_by, recorded_at, db)

                            if projector_result.valid_event:
                                log.warning(f"[INVITE_ACCEPTED_BOOTSTRAP] projected network event {network_id[:20]}...")
                                # Mark network as valid
                                safedb.execute(
                                    "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
                                    (network_id, recorded_by)
                                )
                                # Notify blocked queue to unblock events waiting on network
                                unblocked_by_network = queues.blocked.notify_event_valid(network_id, recorded_by, safedb)
                                if unblocked_by_network:
                                    log.warning(f"[INVITE_ACCEPTED_BOOTSTRAP] unblocked {len(unblocked_by_network)} events after network became valid")
                                    # Re-project unblocked events recursively
                                    recorded_module.project_ids(unblocked_by_network, db)
                            else:
                                log.warning(f"[INVITE_ACCEPTED_BOOTSTRAP] network projection returned invalid for {network_id[:20]}...")
                    else:
                        log.warning(f"[INVITE_ACCEPTED_BOOTSTRAP] network resolve failed: {resolve_result.status} - {resolve_result.error}")
            else:
                log.debug(f"[INVITE_ACCEPTED_BOOTSTRAP] network already projected {network_id[:20]}...")

    # Store inviter's peer_shared blob from link data (for sync)
    if inviter_peer_shared_blob_id and inviter_peer_shared_id:
        inviter_blob = store.get(inviter_peer_shared_blob_id, unsafedb)
        if not inviter_blob:
            log.warning("[INVITE_ACCEPTED_BOOTSTRAP] inviter peer_shared blob_id not found")
        else:
            if inviter_peer_shared_blob_id != inviter_peer_shared_id:
                log.warning(
                    f"[INVITE_ACCEPTED_BOOTSTRAP] inviter peer_shared blob id mismatch: "
                    f"{inviter_peer_shared_blob_id} != {inviter_peer_shared_id}"
                )
            ps_recorded_id = recorded_module.create(
                inviter_peer_shared_blob_id, recorded_by, recorded_at, db, return_dupes=False
            )
            log.warning("[INVITE_ACCEPTED_BOOTSTRAP] created recorded wrapper for inviter peer_shared")
    elif inviter_peer_shared_blob_b64 and inviter_peer_shared_id:
        padding = 4 - len(inviter_peer_shared_blob_b64) % 4
        if padding != 4:
            inviter_peer_shared_blob_b64 += '=' * padding
        inviter_peer_shared_blob = base64.urlsafe_b64decode(inviter_peer_shared_blob_b64)

        stored_ps_id = store.blob(inviter_peer_shared_blob, recorded_at, True, unsafedb)
        log.warning(f"[INVITE_ACCEPTED_BOOTSTRAP] stored inviter peer_shared blob, id={stored_ps_id[:20]}...")

        # Create recorded wrapper for inviter's peer_shared so it can be projected
        ps_recorded_id = recorded_module.create(stored_ps_id, recorded_by, recorded_at, db, return_dupes=False)
        log.warning(f"[INVITE_ACCEPTED_BOOTSTRAP] created recorded wrapper for inviter peer_shared")


# Register command handler at module load
register_command_handler('handle_invite_accepted_bootstrap', _handle_invite_accepted_bootstrap)
