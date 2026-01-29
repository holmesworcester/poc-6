"""Message rekey event type for forward secrecy.

When a message is deleted, its encryption key is marked for purging.
This event type allows re-encrypting messages with a new "clean" key
before the old key is discarded, ensuring forward secrecy.
"""

# Registry metadata
EVENT_TYPE = 'message_rekey'
SHAREABLE = True  # Rekey events sync for forward secrecy
PROJECTION_TABLE = None

# Wire format constants
WIRE_TYPE_CODE = 0x09  # TYPE_MESSAGE_REKEY
WIRE_PLAINTEXT_SIZE = 400  # MESSAGE_REKEY_PLAINTEXT_SIZE
MESSAGE_REKEY_CIPHERTEXT_MAX = WIRE_PLAINTEXT_SIZE - 34

# v2 event specification
EVENT_SPEC = {
    'encrypted': False,  # Plain JSON, not group-wrapped
    'signer': None,  # signed_by is peer_id, no cryptographic verification
    'requires': {},  # Validates key exists in project()
    'optional': {},
    'cascade_on_delete': [],
}

from typing import Any
import logging
import struct
from core import crypto
from core import store
from core import wire_format
from core.projection.types import ProjectorResult, WriteOp, Command
from core.projection.apply import register_command_handler
from events.group import group_key
from events.content import message as message_mod
from core.db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


# Wire format functions - encode/decode for message_rekey event type

def encode_plaintext(
    original_message_id: bytes,
    new_key_id: bytes,
    new_ciphertext: bytes,
) -> bytes:
    """Encode a message_rekey payload plaintext (pre-encryption)."""
    wire_format._require_len("original_message_id", original_message_id, 16)
    wire_format._require_len("new_key_id", new_key_id, 16)
    new_ciphertext = bytes(new_ciphertext)
    if len(new_ciphertext) > MESSAGE_REKEY_CIPHERTEXT_MAX:
        raise ValueError(
            f"new_ciphertext exceeds {MESSAGE_REKEY_CIPHERTEXT_MAX} bytes, got {len(new_ciphertext)}"
        )
    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:16] = original_message_id
    payload[16:32] = new_key_id
    struct.pack_into("<H", payload, 32, len(new_ciphertext))
    payload[34:34 + len(new_ciphertext)] = new_ciphertext
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a message_rekey payload plaintext (post-decryption)."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(f"message_rekey plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}")
    original_message_id = data[0:16]
    new_key_id = data[16:32]
    (ciphertext_len,) = struct.unpack_from("<H", data, 32)
    if ciphertext_len > MESSAGE_REKEY_CIPHERTEXT_MAX:
        raise ValueError(f"new_ciphertext_len exceeds {MESSAGE_REKEY_CIPHERTEXT_MAX}, got {ciphertext_len}")
    new_ciphertext = data[34:34 + ciphertext_len]
    return {
        "original_message_id": original_message_id,
        "new_key_id": new_key_id,
        "new_ciphertext": new_ciphertext,
    }


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a message_rekey wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(
    *,
    original_message_id_b64: str,
    new_key_id_b64: str,
    new_ciphertext: bytes,
    signed_by_b64: str,
    created_at_ms: int,
) -> bytes:
    """Encode a complete message_rekey wire event."""
    original_message_id = crypto.b64decode(original_message_id_b64)
    new_key_id = crypto.b64decode(new_key_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_plaintext(
        original_message_id=original_message_id,
        new_key_id=new_key_id,
        new_ciphertext=new_ciphertext,
    )
    header = wire_format.WireHeader(
        version=1,
        event_type=WIRE_TYPE_CODE,
        flags=wire_format.FLAG_UNSIGNED,
        signer_type=wire_format.SIGNER_PEER,
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=wire_format._require_len("signer_id", signer_id, wire_format.SIGNER_ID_SIZE),
    )
    payload = wire_format._pad_payload(plaintext)
    signature = b"\x00" * wire_format.SIGNATURE_SIZE
    return wire_format.build_envelope(header, payload, signature)


def decode_wire_event(data: bytes) -> dict[str, Any]:
    """Decode a message_rekey wire event."""
    header, payload, _signature = wire_format.parse_envelope(data)
    if header.event_type != WIRE_TYPE_CODE:
        raise ValueError("unexpected event type for message_rekey")
    plaintext = payload[:WIRE_PLAINTEXT_SIZE]
    decoded = decode_plaintext(plaintext)
    return {
        "type": "message_rekey",
        "original_message_id": crypto.b64encode(decoded["original_message_id"]),
        "new_key_id": crypto.b64encode(decoded["new_key_id"]),
        "new_ciphertext": crypto.b64encode(decoded["new_ciphertext"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": wire_format.signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_ciphertext_only": True,
    }


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for message_rekey events.

    Validates the rekey can be decrypted, then returns writes for
    message_rekeys table and a command to replace the store blob.
    """
    event_data = ctx.event_data

    original_message_id = event_data.get('original_message_id')
    new_key_id = event_data.get('new_key_id')
    new_ciphertext_and_nonce_b64 = event_data.get('new_ciphertext')
    created_at = event_data.get('created_at')

    if not all([original_message_id, new_key_id, new_ciphertext_and_nonce_b64, created_at is not None]):
        log.warning(f"message_rekey.project_pure() missing required fields")
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_message_rekey(original_message_id, new_key_id, new_ciphertext_and_nonce_b64)

    # Decode ciphertext and nonce
    new_ciphertext_and_nonce = crypto.b64decode(new_ciphertext_and_nonce_b64)
    if event_data.get("_wire_ciphertext_only"):
        nonce_hint = f"{original_message_id}{new_key_id}".encode('utf-8')
        nonce = crypto.hash(nonce_hint, size=crypto.NONCE_SIZE)
        new_ciphertext = new_ciphertext_and_nonce
    else:
        if len(new_ciphertext_and_nonce) <= crypto.NONCE_SIZE:
            log.warning("message_rekey.project_pure() new_ciphertext missing nonce")
            return ProjectorResult(writes=tuple(), valid_event=False)
        nonce = new_ciphertext_and_nonce[-crypto.NONCE_SIZE:]
        new_ciphertext = new_ciphertext_and_nonce[:-crypto.NONCE_SIZE]

    writes = (
        WriteOp(
            op='insert',
            table='message_rekeys',
            values={
                'rekey_id': ctx.event_id,
                'original_message_id': original_message_id,
                'new_key_id': new_key_id,
                'new_ciphertext': new_ciphertext,
                'created_at': created_at,
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
        WriteOp(
            op='update',
            table='messages',
            values={'key_id': new_key_id},
            where={
                'message_id': original_message_id,
                'recorded_by': ctx.recorded_by,
            },
        ),
    )

    # Command to replace the blob in store and validate decryption
    commands = (
        Command(
            command_type='replace_message_blob',
            args={
                'original_message_id': original_message_id,
                'new_key_id': new_key_id,
                'new_ciphertext': new_ciphertext,
                'nonce': nonce,
            }
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True, commands=commands)


def _handle_replace_message_blob(args: dict, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Handle blob replacement command.

    Validates the rekey can be decrypted with the new key,
    then replaces the original message blob in the store.
    """
    original_message_id = args['original_message_id']
    new_key_id = args['new_key_id']
    new_ciphertext = args['new_ciphertext']
    nonce = args['nonce']

    safedb = create_safe_db(db, recorded_by=recorded_by)
    unsafedb = create_unsafe_db(db)

    # Get new key to validate
    new_key_row = safedb.query_one(
        "SELECT key FROM group_keys WHERE key_id = ? AND recorded_by = ? LIMIT 1",
        (new_key_id, recorded_by)
    )
    if not new_key_row:
        log.warning(f"message_rekey: new key {new_key_id[:20]}... not found, skipping blob replacement")
        return

    # Validate decryption works
    try:
        new_key = new_key_row['key']
        rekeyed_plaintext = crypto.decrypt(new_ciphertext, new_key, nonce)
        log.info(f"message_rekey: verified rekeyed plaintext, size={len(rekeyed_plaintext)}B")
    except Exception as e:
        log.warning(f"message_rekey: failed to decrypt rekeyed message: {e}")
        return

    # Replace original message blob in store with new ciphertext
    new_key_id_bytes = crypto.b64decode(new_key_id)
    original_blob = store.get(original_message_id, unsafedb)
    if not message_mod.is_wire_envelope(original_blob):
        log.warning("message_rekey: non-wire message blob, skipping blob replacement")
        return
    header, _payload, signature = wire_format.parse_envelope(original_blob)
    new_payload = new_key_id_bytes + nonce + new_ciphertext
    if len(new_payload) != wire_format.PAYLOAD_SIZE:
        log.warning("message_rekey: unexpected wire payload size, skipping blob replacement")
        return
    new_blob = header.pack() + new_payload + signature

    unsafedb.execute(
        "UPDATE store SET blob = ? WHERE id = ?",
        (new_blob, original_message_id)
    )
    log.info(f"message_rekey: replaced message blob in store for {original_message_id[:20]}...")


# Register the command handler at module load time
register_command_handler('replace_message_blob', _handle_replace_message_blob)


def create(original_message_id: str, new_key_id: str, peer_id: str, t_ms: int, db: Any) -> str:
    """Create a message_rekey event to re-encrypt a message with a new key.

    Uses deterministic nonce to ensure same plaintext + key -> same ciphertext,
    allowing peer convergence without needing to exchange the rekeyed plaintext.

    Args:
        original_message_id: Message event ID to re-encrypt
        new_key_id: New key to use for re-encryption
        peer_id: Local peer ID creating the rekey
        t_ms: Timestamp
        db: Database connection

    Returns:
        rekey_id: The stored message_rekey event ID

    Raises:
        ValueError: If message or key not found
    """
    log.info(f"message_rekey.create() rekeying message_id={original_message_id[:20]}... with new key={new_key_id[:20]}...")

    unsafedb = create_unsafe_db(db)

    # Get original message blob from store
    original_blob = store.get(original_message_id, unsafedb)
    if not original_blob:
        raise ValueError(f"Original message {original_message_id} not found in store")

    # Unwrap (decrypt) original message to get plaintext
    if not message_mod.is_wire_envelope(original_blob):
        raise ValueError(f"Non-wire message blob for {original_message_id} cannot be rekeyed")
    header, payload, _signature = wire_format.parse_envelope(original_blob)
    if header.event_type != wire_format.TYPE_MESSAGE:
        raise ValueError(f"Event {original_message_id} is not a message, cannot rekey")
    key_id = payload[:crypto.KEY_ID_SIZE]
    key_data = crypto.get_key_by_id(key_id, peer_id, db)
    if not key_data:
        raise ValueError(f"Cannot decrypt original message {original_message_id} - missing key")
    plaintext = message_mod._decrypt_payload(payload, key_data)

    log.info(f"message_rekey.create() decrypted original message, plaintext_size={len(plaintext)}B")

    # Get the new key from group_keys table
    new_key_row = group_key.get_key(new_key_id, peer_id, db)
    if not new_key_row:
        raise ValueError(f"New key {new_key_id} not found for peer {peer_id}")

    new_key = new_key_row['key']
    log.info(f"message_rekey.create() found new key {new_key_id[:20]}...")

    # Re-encrypt with deterministic nonce
    # Nonce = BLAKE2b(original_message_id + new_key_id)
    nonce_hint = f"{original_message_id}{new_key_id}".encode('utf-8')
    deterministic_nonce = crypto.hash(nonce_hint, size=crypto.NONCE_SIZE)

    new_ciphertext = crypto.encrypt(plaintext, new_key, deterministic_nonce)
    log.info(f"message_rekey.create() re-encrypted with deterministic nonce, ciphertext_size={len(new_ciphertext)}B")

    # Create rekey event record (plaintext, no additional encryption)
    blob = encode_wire_event(
        original_message_id_b64=original_message_id,
        new_key_id_b64=new_key_id,
        new_ciphertext=new_ciphertext,
        signed_by_b64=peer_id,
        created_at_ms=t_ms,
    )
    rekey_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"message_rekey.create() created rekey_id={rekey_id[:20]}...")
    return rekey_id


def _wire_shadow_message_rekey(original_message_id: str, new_key_id: str, new_ciphertext_b64: str) -> None:
    """Validate message_rekey fields against the fixed-size wire payload layout."""
    plaintext = encode_plaintext(
        original_message_id=crypto.b64decode(original_message_id),
        new_key_id=crypto.b64decode(new_key_id),
        new_ciphertext=crypto.b64decode(new_ciphertext_b64),
    )
    decoded = decode_plaintext(plaintext)
    if decoded["new_key_id"] != crypto.b64decode(new_key_id):
        raise ValueError("wire shadow decode new_key_id mismatch")
