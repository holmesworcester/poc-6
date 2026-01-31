"""Network name update event type (encrypted name update for networks).

In sender key model, network_name_update is encrypted with sender keys to the main group.
"""

# Registry metadata
EVENT_TYPE = 'network_name_update'
SHAREABLE = True  # Name updates sync across network
PROJECTION_TABLE = None

# Wire format constants
WIRE_TYPE_CODE = 0x28  # TYPE_NETWORK_NAME_UPDATE
WIRE_PLAINTEXT_SIZE = 344  # NETWORK_NAME_UPDATE_PLAINTEXT_SIZE
NAME_MAX = 64

from typing import Any
import logging
import struct
from core import crypto
from core import store
from core import wire_format
from events.group import group, sender_key
from events.identity import peer
from core.db import create_safe_db, create_unsafe_db
from core.projection.types import ProjectorResult, WriteOp

EVENT_SPEC = {
    'encrypted': True,  # Encrypted with sender keys
    'signer': {
        'id_field': 'signed_by',
        'type_field': 'signer_type',
    },
    'requires': {
        'network': {
            'source': 'table',
            'table': 'networks',
            'key': 'network_id',
            'fields': ['network_id'],
        },
    },
    'optional': {
        'existing_name': {
            'source': 'table',
            'table': 'network_names',
            'key': 'network_id',
            'key_from': 'network_id',
            'fields': ['network_id', 'global_count'],
        },
    },
    'cascade_on_delete': [],
}

log = logging.getLogger(__name__)


# Wire format functions - encode/decode for network_name_update event type

def encode_plaintext(network_id: bytes, name: str | bytes) -> bytes:
    """Encode a network_name_update payload plaintext (pre-encryption).

    Layout (344 bytes):
    - network_id (16)
    - name_len (u16)
    - name_bytes (NAME_MAX)
    - pad
    """
    wire_format._require_len("network_id", network_id, 16)
    if isinstance(name, str):
        name_bytes = name.encode("utf-8")
    else:
        name_bytes = bytes(name)
    if len(name_bytes) > NAME_MAX:
        raise ValueError(f"name exceeds {NAME_MAX} bytes, got {len(name_bytes)}")
    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:16] = network_id
    struct.pack_into("<H", payload, 16, len(name_bytes))
    payload[18:18 + len(name_bytes)] = name_bytes
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a network_name_update payload plaintext (post-decryption)."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(
            f"network_name_update plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    network_id = data[0:16]
    (name_len,) = struct.unpack_from("<H", data, 16)
    if name_len > NAME_MAX:
        raise ValueError(f"name_len exceeds {NAME_MAX}, got {name_len}")
    name_bytes = data[18:18 + name_len]
    try:
        name = name_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("name is not valid utf-8") from exc
    return {"network_id": network_id, "name": name}


def _encrypt_payload(plaintext: bytes, key_data: dict[str, Any]) -> bytes:
    """Encrypt network_name_update plaintext into wire payload."""
    if len(plaintext) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(
            f"network_name_update plaintext must be {WIRE_PLAINTEXT_SIZE} bytes"
        )
    key_id = wire_format._require_len("key_id", key_data.get("id", b""), 16)
    if key_data.get("type") != "symmetric":
        raise ValueError("network_name_update payload requires symmetric key")
    nonce = crypto.deterministic_nonce(key_id, plaintext)
    ciphertext = crypto.encrypt(plaintext, key_data["key"], nonce)
    if len(ciphertext) != WIRE_PLAINTEXT_SIZE + 16:
        raise ValueError("unexpected ciphertext length for network_name_update payload")
    payload = key_id + nonce + ciphertext
    return wire_format._require_len("payload", payload, wire_format.PAYLOAD_SIZE)


def _decrypt_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    """Decrypt wire payload to network_name_update plaintext."""
    if key_data.get("type") != "symmetric":
        raise ValueError("network_name_update payload requires symmetric key")
    key_id = payload[:16]
    nonce = payload[16:40]
    ciphertext = payload[40:]
    return crypto.decrypt(ciphertext, key_data["key"], nonce)


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a network_name_update wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(
    *,
    network_id_b64: str,
    name: str,
    signed_by_b64: str,
    signer_type: str,
    global_count: int,
    created_at_ms: int,
    key_data: dict[str, Any],
    private_key: bytes,
) -> bytes:
    """Encode a complete network_name_update wire event."""
    if global_count < 0 or global_count > 0xFFFFFFFF:
        raise ValueError("global_count must fit in u32")
    network_id = crypto.b64decode(network_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_plaintext(network_id=network_id, name=name)
    header = wire_format.WireHeader(
        version=1,
        event_type=WIRE_TYPE_CODE,
        flags=wire_format.FLAG_ENCRYPTED,
        signer_type=wire_format.signer_type_from_str(signer_type),
        count=global_count,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=wire_format._require_len("signer_id", signer_id, wire_format.SIGNER_ID_SIZE),
    )
    signed_bytes = wire_format._signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _encrypt_payload(plaintext, key_data)
    return wire_format.build_envelope(header, payload, signature)


def decode_wire_event(
    data: bytes,
    recorded_by: str,
    db: Any,
    key_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Decode a network_name_update wire event."""
    header, payload, signature = wire_format.parse_envelope(data)
    if header.event_type != WIRE_TYPE_CODE:
        return None, []
    if header.flags & wire_format.FLAG_ENCRYPTED:
        key_id = payload[:16]
        key_id_b64 = crypto.b64encode(key_id)
        key_data = crypto.get_key_by_id(key_id, recorded_by, db, key_cache=key_cache)
        if not key_data:
            return None, [key_id_b64]
        plaintext = _decrypt_payload(payload, key_data)
    else:
        plaintext = payload[:WIRE_PLAINTEXT_SIZE]

    decoded = decode_plaintext(plaintext)
    event_data = {
        "type": EVENT_TYPE,
        "network_id": crypto.b64encode(decoded["network_id"]),
        "name": decoded["name"],
        "key_id": crypto.b64encode(payload[:16]),
        "global_count": header.count,
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": wire_format.signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": wire_format._signing_bytes(header, plaintext),
    }
    return event_data, []


def _wire_shadow_network_name_update(network_id: str, name: str) -> None:
    """Validate network_name_update fields against the fixed-size wire payload layout."""
    plaintext = encode_plaintext(
        network_id=crypto.b64decode(network_id),
        name=name,
    )
    decoded = decode_plaintext(plaintext)
    if decoded["name"] != name:
        raise ValueError("wire shadow decode name mismatch")


def create(network_id: str, name: str, peer_id: str, peer_shared_id: str, t_ms: int,
           db: Any) -> str:
    """Create a network_name_update event.

    In sender key model, network_name_update is encrypted with sender keys to the main group.

    Args:
        network_id: The network event ID this name updates
        name: Network name
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (for created_by)
        t_ms: Timestamp
        db: Database connection

    Returns:
        network_name_update_id: The stored event ID

    Raises:
        KeyNotAvailableError: If main group not available yet
    """
    log.info(f"network_name_update.create() creating network name for network_id={network_id[:20]}..., name='{name}'")

    # Get main group (all_members) - use is_main flag since name varies
    main_group = group.get_main(peer_id, db)
    if not main_group:
        log.info(f"network_name_update.create() main group not found yet")
        raise KeyNotAvailableError("Main group not available yet - will be created on sync")

    group_id = main_group['group_id']

    # Get or create sender key for main group
    key_data = sender_key.pick_or_create_key(
        group_id=group_id,
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        t_ms=t_ms,
        db=db,
    )

    _wire_shadow_network_name_update(network_id, name)

    private_key = peer.get_private_key(peer_id, peer_id, db)
    blob = encode_wire_event(
        network_id_b64=network_id,
        name=name,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        global_count=0,
        created_at_ms=t_ms,
        key_data=key_data,
        private_key=private_key,
    )
    network_name_update_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"network_name_update.create() created network_name_update_id={network_name_update_id[:20]}...")
    return network_name_update_id


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for network_name_update events."""
    event_data = ctx.event_data

    if event_data.get('type') != 'network_name_update':
        return ProjectorResult(writes=tuple(), valid_event=False)

    network_id = event_data.get('network_id')
    name = event_data.get('name')
    key_id = event_data.get('key_id')
    global_count = event_data.get('global_count', 0)

    if not network_id or not name:
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_network_name_update(network_id, name)

    existing = ctx.deps.get('existing_name')
    writes: list[WriteOp] = []

    if existing is None:
        writes.append(
            WriteOp(
                op='insert',
                table='network_names',
                values={
                    'network_id': network_id,
                    'name': name,
                    'event_id': ctx.event_id,
                    'global_count': global_count,
                    'key_id': key_id,
                    'created_at': event_data.get('created_at'),
                    'signed_by': event_data.get('signed_by'),
                    'recorded_at': ctx.recorded_at,
                },
            )
        )
    elif existing.get('global_count', 0) < global_count:
        writes.append(
            WriteOp(
                op='update',
                table='network_names',
                values={
                    'name': name,
                    'event_id': ctx.event_id,
                    'global_count': global_count,
                    'key_id': key_id,
                    'created_at': event_data.get('created_at'),
                    'signed_by': event_data.get('signed_by'),
                    'recorded_at': ctx.recorded_at,
                },
                where={
                    'network_id': network_id,
                },
            )
        )

    return ProjectorResult(writes=tuple(writes), valid_event=True)


def validate(event_id: str, recorded_by: str, db: Any) -> str | None:
    """Validate a network_name_update event.

    Validation checks:
    1. Does the network event (with event_id = network_id field) exist?

    Args:
        event_id: The network_name_update event ID
        recorded_by: The peer that recorded this event
        db: Database connection

    Returns:
        'VALID' if network exists and valid
        'BLOCKED' if network doesn't exist yet (wait for network event)
        None if invalid (can't recover)
    """
    log.debug(f"network_name_update.validate() validating {event_id[:20]}...")

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get the event blob to extract network_id field
    blob = store.get(event_id, db)
    if not blob:
        log.warning(f"network_name_update.validate() blob not found for {event_id[:20]}...")
        return None

    try:
        if not is_wire_envelope(blob):
            log.warning(f"network_name_update.validate() non-wire event blob for {event_id[:20]}...")
            return None
        event_data, missing = decode_wire_event(blob, recorded_by, db)
        if not event_data:
            return 'BLOCKED' if missing else None
    except Exception as e:
        log.warning(f"network_name_update.validate() failed to parse event: {e}")
        return None

    # Extract network_id field
    network_id = event_data.get('network_id')
    if not network_id:
        log.warning(f"network_name_update.validate() missing network_id field")
        return None

    # Check: Does the network event exist?
    network_event = safedb.query_one(
        "SELECT network_id FROM networks WHERE network_id = ? AND recorded_by = ? LIMIT 1",
        (network_id, recorded_by)
    )

    if not network_event:
        log.debug(f"network_name_update.validate() network not found, blocking: {network_id[:20]}...")
        return "BLOCKED"

    log.debug(f"network_name_update.validate() valid: network exists")
    return "VALID"


class KeyNotAvailableError(Exception):
    """Raised when group key is not available for encryption."""
    pass
