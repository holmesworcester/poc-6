"""Crypto functions for wrapping and unwrapping."""
from typing import Any, Tuple
import base64
import json

import nacl.secret
import nacl.hash
import nacl.signing
import nacl.encoding
import nacl.utils


def parse_json(data: bytes) -> dict[str, Any]:
    """Parse JSON from bytes."""
    return json.loads(data.decode('utf-8'))
from nacl.public import SealedBox, Box, PrivateKey, PublicKey

from . import store

# ===== Constants =====

KEY_ID_SIZE = 16  # bytes (128 bits) - BLAKE2b-128 for key IDs
SECRET_SIZE = 32  # bytes (256 bits) for symmetric keys
NONCE_SIZE = 24  # bytes (192 bits) for XChaCha20-Poly1305

# ===== Crypto Primitives =====

def generate_secret() -> bytes:
    """Generate a random 32-byte secret."""
    return nacl.utils.random(SECRET_SIZE)


def generate_keypair() -> Tuple[bytes, bytes]:
    """Generate an Ed25519 keypair. Returns (private_key, public_key)."""
    signing_key = nacl.signing.SigningKey.generate()
    return bytes(signing_key), bytes(signing_key.verify_key)


def hash(data: bytes, size: int = KEY_ID_SIZE) -> bytes:
    """BLAKE2b hash. Default 16 bytes (128 bits) for key IDs."""
    return nacl.hash.blake2b(data, digest_size=size, encoder=nacl.encoding.RawEncoder)


def kdf(secret: bytes, salt: bytes, size: int = SECRET_SIZE) -> bytes:
    """Key derivation function using BLAKE2b with salt.

    Used for deriving invite pubkeys from invite secrets.
    Default size is 32 bytes (256 bits).
    """
    return nacl.hash.blake2b(secret, salt=salt, digest_size=size, encoder=nacl.encoding.RawEncoder)


def sign(message: bytes, private_key: bytes) -> bytes:
    """Sign a message with Ed25519."""
    signing_key = nacl.signing.SigningKey(private_key)
    signed = signing_key.sign(message)
    return signed.signature


def verify(message: bytes, signature: bytes, public_key: bytes) -> bool:
    """Verify an Ed25519 signature."""
    try:
        verify_key = nacl.signing.VerifyKey(public_key)
        verify_key.verify(message, signature)
        return True
    except nacl.exceptions.BadSignatureError:
        return False


def encrypt(plaintext: bytes, key: bytes, nonce: bytes) -> bytes:
    """Encrypt with XChaCha20-Poly1305 using provided nonce. Returns ciphertext only."""
    box = nacl.secret.SecretBox(key)
    encrypted = box.encrypt(plaintext, nonce)
    # PyNaCl prepends nonce, extract only ciphertext
    return encrypted[NONCE_SIZE:]


def decrypt(ciphertext: bytes, key: bytes, nonce: bytes) -> bytes:
    """Decrypt with XChaCha20-Poly1305."""
    box = nacl.secret.SecretBox(key)
    # PyNaCl expects nonce prepended
    return box.decrypt(nonce + ciphertext)


def seal(plaintext: bytes, public_key: bytes) -> bytes:
    """Deterministically seal a message to a public key.

    In a content-addressed system, identical content produces identical event IDs,
    so random nonces provide no security benefit (identical ciphertext = same event,
    deduplicated). We derive the ephemeral keypair deterministically from the inputs
    to enable reproducible event creation.

    Output format is SealedBox-compatible: ephemeral_public_key || Box(plaintext)
    """
    # Convert Ed25519 to X25519 for encryption
    verify_key = nacl.signing.VerifyKey(public_key)
    recipient_curve = verify_key.to_curve25519_public_key()

    # Derive ephemeral keypair deterministically from recipient + plaintext
    # This ensures: same recipient + same plaintext → same ciphertext → same event_id
    seed = hash(bytes(recipient_curve) + plaintext, size=32)  # PrivateKey needs 32 bytes
    ephemeral_private = PrivateKey(seed)
    ephemeral_public = ephemeral_private.public_key

    # Encrypt with Box (ephemeral → recipient)
    box = Box(ephemeral_private, recipient_curve)
    # Derive nonce deterministically from plaintext
    nonce = hash(plaintext, size=NONCE_SIZE)
    encrypted = box.encrypt(plaintext, nonce)
    # Box.encrypt returns nonce + ciphertext, extract just ciphertext
    ciphertext = encrypted[NONCE_SIZE:]

    # SealedBox format: ephemeral_public || nonce || ciphertext
    return bytes(ephemeral_public) + nonce + ciphertext


def unseal(sealed_data: bytes, private_key: bytes) -> bytes:
    """Unseal a deterministically sealed message with a private key.

    Expects format: ephemeral_public (32) || nonce (24) || ciphertext
    """
    # Convert Ed25519 to X25519 for decryption
    signing_key = nacl.signing.SigningKey(private_key)
    recipient_private = signing_key.to_curve25519_private_key()

    # Parse sealed format: ephemeral_public || nonce || ciphertext
    ephemeral_public = PublicKey(sealed_data[:32])
    nonce = sealed_data[32:32 + NONCE_SIZE]
    ciphertext = sealed_data[32 + NONCE_SIZE:]

    # Decrypt with Box
    box = Box(recipient_private, ephemeral_public)
    return box.decrypt(ciphertext, nonce)


def b64encode(data: bytes) -> str:
    """Encode bytes to base64 ASCII string."""
    return base64.b64encode(data).decode('ascii')


def b64decode(data: str) -> bytes:
    """Decode base64 ASCII string to bytes."""
    return base64.b64decode(data)




def deterministic_nonce(hint: bytes, plaintext_bytes: bytes) -> bytes:
    """Derive a deterministic nonce from hint and canonical plaintext.

    This makes encryption deterministic: same plaintext + key → same ciphertext.
    Enables content-addressing and deduplication.
    """
    return hash(hint + plaintext_bytes, size=NONCE_SIZE)


# ===== Key Lookup Functions =====


def extract_id(blob: bytes) -> bytes:
    """Extract the key ID from a wrapped blob (first KEY_ID_SIZE bytes)."""
    return blob[:KEY_ID_SIZE]


def get_transit_key_by_id(id_bytes: bytes, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get transit decryption key. Only checks LOCAL key tables with our private keys.

    Checks:
    - connections WHERE our_key and key_id matches
    - connection_prekeys WHERE owner_peer_id == recorded_by
    - local_peers.private_key WHERE peer_id == recorded_by

    Does NOT check:
    - connection_prekeys_shared (public keys for encryption, not decryption)
    - group_keys, group_prekeys (wrong namespace)

    Args:
        id_bytes: Key ID bytes (16 bytes) - key ID from blob
        recorded_by: Peer ID attempting to decrypt (ownership filter)
        db: Database connection

    Returns:
        Key dict for crypto.unwrap_transit(), or None if not found or not owned
    """
    import logging
    from .db import create_unsafe_db

    log = logging.getLogger(__name__)
    key_id = b64encode(id_bytes)
    unsafedb = create_unsafe_db(db)

    log.debug(f"get_transit_key_by_id() looking up key_id={key_id}, recorded_by={recorded_by[:20]}...")

    # Check connection_prekeys (asymmetric) - DIRECT lookup, no _shared table
    transit_prekey_row = unsafedb.query_one(
        "SELECT private_key FROM connection_prekeys WHERE connection_prekey_id = ? AND owner_peer_id = ?",
        (key_id, recorded_by)
    )
    if transit_prekey_row and transit_prekey_row['private_key']:
        log.debug(f"get_transit_key_by_id() found transit prekey for prekey_id={key_id}")
        return {
            'id': id_bytes,
            'private_key': transit_prekey_row['private_key'],
            'type': 'asymmetric'
        }

    # Check local_peers (peer private key)
    if key_id == recorded_by:
        peer_row = unsafedb.query_one(
            "SELECT private_key FROM local_peers WHERE peer_id = ?",
            (key_id,)
        )
        if peer_row and peer_row['private_key']:
            log.debug(f"get_transit_key_by_id() found peer private key for peer_id={key_id}")
            return {
                'id': id_bytes,
                'private_key': peer_row['private_key'],
                'type': 'asymmetric'
            }

    # Check connections table (symmetric keys from connection handshake)
    # key_id IS the connection identifier - direct indexed lookup
    from .db import create_safe_db
    safedb = create_safe_db(db, recorded_by=recorded_by)
    conn_row = safedb.query_one(
        "SELECT our_key FROM connections WHERE key_id = ? AND recorded_by = ?",
        (key_id, recorded_by)
    )
    if conn_row:
        log.debug(f"get_transit_key_by_id() found connection key for key_id={key_id[:20]}...")
        return {
            'id': id_bytes,
            'key': conn_row['our_key'],
            'type': 'symmetric'
        }

    log.debug(f"get_transit_key_by_id() NO KEY FOUND for key_id={key_id}, recorded_by={recorded_by[:20]}...")
    return None


def get_event_key_by_id(id_bytes: bytes, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get event decryption key. Only checks LOCAL key tables with our private keys.

    Checks:
    - group_keys WHERE recorded_by == recorded_by
    - group_prekeys WHERE owner_peer_id == recorded_by AND recorded_by == recorded_by

    Does NOT check:
    - group_prekeys_shared (public keys for encryption, not decryption)
    - transit_keys, connection_prekeys (wrong namespace)

    Args:
        id_bytes: Key ID bytes (16 bytes) - key ID from blob
        recorded_by: Peer ID attempting to decrypt (ownership filter)
        db: Database connection

    Returns:
        Key dict for crypto.unwrap_event(), or None if not found or not owned
    """
    import logging
    from .db import create_safe_db

    log = logging.getLogger(__name__)
    key_id = b64encode(id_bytes)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    log.debug(f"get_event_key_by_id() looking up key_id={key_id}, recorded_by={recorded_by[:20]}...")

    # Check group_keys (symmetric)
    group_row = safedb.query_one(
        "SELECT key FROM group_keys WHERE key_id = ? AND recorded_by = ?",
        (key_id, recorded_by)
    )
    if group_row:
        log.debug(f"get_event_key_by_id() found group key for key_id={key_id}")
        return {
            'id': id_bytes,
            'key': group_row['key'],
            'type': 'symmetric'
        }

    # Check group_prekeys (asymmetric) - DIRECT lookup by prekey_id
    # Hint is now group_prekey_id (matches local prekey_id), consistent with transit_prekey
    group_prekey_row = safedb.query_one(
        "SELECT private_key FROM group_prekeys WHERE prekey_id = ? AND owner_peer_id = ? AND recorded_by = ?",
        (key_id, recorded_by, recorded_by)
    )
    if group_prekey_row and group_prekey_row['private_key']:
        log.debug(f"get_event_key_by_id() found group prekey for prekey_id={key_id}")
        return {
            'id': id_bytes,
            'private_key': group_prekey_row['private_key'],
            'type': 'asymmetric'
        }

    log.debug(f"get_event_key_by_id() NO KEY FOUND for key_id={key_id}, recorded_by={recorded_by[:20]}...")
    return None


def get_key_by_id(id_bytes: bytes, recorded_by: str, db: Any,
                  key_cache: dict[str, dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Get key from database by id bytes. Checks transit keys, group keys, and prekeys.

    Args:
        id_bytes: Key ID bytes (16 bytes) - key ID from blob
        recorded_by: Peer ID attempting to access this key (for ownership filtering)
        db: Database connection

    Returns:
        Key dict for crypto.unwrap(), or None if not found or not accessible
    """
    import logging
    from .db import create_unsafe_db, create_safe_db

    log = logging.getLogger(__name__)
    key_id = b64encode(id_bytes)
    if key_cache is not None:
        cached = key_cache.get(key_id)
        if cached:
            return cached
    unsafedb = create_unsafe_db(db)

    log.debug(f"get_key_by_id() looking up key_id={key_id}, recorded_by={recorded_by[:20]}...")

    # First try connections table (symmetric keys from connection handshake)
    # key_id IS the connection identifier - direct indexed lookup
    safedb = create_safe_db(db, recorded_by=recorded_by)
    conn_row = safedb.query_one(
        "SELECT our_key FROM connections WHERE key_id = ? AND recorded_by = ?",
        (key_id, recorded_by)
    )
    if conn_row:
        log.debug(f"get_key_by_id() found connection key for key_id={key_id[:20]}...")
        return {
            'id': id_bytes,
            'key': conn_row['our_key'],
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

    # Then try asymmetric keys from connection_prekeys (device-wide, with ownership filter)
    # The hint (key_id) is always a prekey_shared_id (event ID)
    # Two cases:
    # 1. Detached prekey (from invite_accepted): prekey_id = prekey_shared_id directly
    # 2. Regular prekey: need to find connection_prekey_id from connection_prekeys_shared, then look up in connection_prekeys

    log.debug(f"get_key_by_id() checking connection_prekeys for prekey_id={key_id} (detached), owner={recorded_by[:20]}...")
    transit_prekey_row = unsafedb.query_one(
        "SELECT private_key FROM connection_prekeys WHERE connection_prekey_id = ? AND owner_peer_id = ? LIMIT 1",
        (key_id, recorded_by)
    )
    if transit_prekey_row and transit_prekey_row['private_key']:
        log.debug(f"get_key_by_id() found transit prekey private key for prekey_id={key_id}")
        return {
            'id': id_bytes,
            'private_key': transit_prekey_row['private_key'],
            'type': 'asymmetric'
        }

    # Try finding via connection_prekeys_shared (for regular prekeys)
    log.debug(f"get_key_by_id() checking connection_prekeys_shared for prekey_shared_id={key_id}")
    connection_prekey_shared_row = safedb.query_one(
        "SELECT connection_prekey_shared_id FROM connection_prekeys_shared WHERE connection_prekey_shared_id = ? AND recorded_by = ? LIMIT 1",
        (key_id, recorded_by)
    )
    if connection_prekey_shared_row:
        # Need to get connection_prekey_id from event data
        connection_prekey_shared_blob = store.get(key_id, db)
        if connection_prekey_shared_blob:
            from . import wire_format
            if not wire_format.is_wire_connection_prekey_shared_envelope(connection_prekey_shared_blob):
                log.warning(f"get_key_by_id() non-wire connection_prekey_shared blob for {key_id}")
                return None
            connection_prekey_shared_data = wire_format.decode_connection_prekey_shared_wire_event(connection_prekey_shared_blob)
            connection_prekey_id = connection_prekey_shared_data.get('connection_prekey_id')
            if connection_prekey_id:
                transit_prekey_row = unsafedb.query_one(
                    "SELECT private_key FROM connection_prekeys WHERE connection_prekey_id = ? AND owner_peer_id = ? LIMIT 1",
                    (connection_prekey_id, recorded_by)
                )
                if transit_prekey_row and transit_prekey_row['private_key']:
                    log.debug(f"get_key_by_id() found transit prekey via connection_prekeys_shared link")
                    return {
                        'id': id_bytes,
                        'private_key': transit_prekey_row['private_key'],
                        'type': 'asymmetric'
                    }

    # Then try group_prekeys (subjective) - direct lookup by prekey_id
    # Hint is now group_prekey_id (matches local prekey_id), consistent with transit_prekey
    log.debug(f"get_key_by_id() checking group_prekeys for prekey_id={key_id}")
    group_prekey_row = safedb.query_one(
        "SELECT private_key FROM group_prekeys WHERE prekey_id = ? AND owner_peer_id = ? AND recorded_by = ? LIMIT 1",
        (key_id, recorded_by, recorded_by)
    )
    if group_prekey_row and group_prekey_row['private_key']:
        log.debug(f"get_key_by_id() found group prekey for prekey_id={key_id}")
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


# ===== Wrap/Unwrap Functions =====


def _unwrap_common(wrapped_blob: bytes, recorded_by: str, db: Any,
                   key_lookup_fn, block_on_missing: bool,
                   namespace_name: str,
                   key_cache: dict[str, dict[str, Any]] | None = None) -> tuple[bytes | None, list[str]]:
    """Internal helper for unwrapping blobs. Consolidates duplicate code.

    Args:
        wrapped_blob: Encrypted blob to unwrap
        recorded_by: Peer ID attempting to unwrap (for access control)
        db: Database connection
        key_lookup_fn: Function to look up key by ID (namespace-specific)
        block_on_missing: If True, return [key_id] on missing key; if False, return []
        namespace_name: Name for logging (e.g., "transit", "event", "both")

    Returns tuple of (plaintext, missing_key_ids).
    """
    import logging
    log = logging.getLogger(__name__)

    log.debug(f"crypto._unwrap_common({namespace_name}) called with blob size={len(wrapped_blob)}B, recorded_by={recorded_by[:20]}...")

    if wrapped_blob and wrapped_blob[:1] in (b'{', b'['):
        raise ValueError("JSON event blobs are no longer supported")

    # Extract id from blob
    try:
        id_bytes = extract_id(wrapped_blob)
        key_id_b64 = b64encode(id_bytes)
        log.debug(f"crypto._unwrap_common({namespace_name}) extracted key_id={key_id_b64}")
    except Exception as e:
        log.error(f"crypto._unwrap_common({namespace_name}) failed to extract id from blob: {e}")
        return (None, [])

    # Get the key using the namespace-specific lookup function or cache
    key_data = None
    if key_cache is not None:
        key_data = key_cache.get(key_id_b64)
    if not key_data:
        key_data = key_lookup_fn(id_bytes, recorded_by, db)
    if not key_data:
        key_id_b64 = b64encode(id_bytes)
        if block_on_missing:
            log.warning(f"crypto._unwrap_common({namespace_name}) key not found for id={key_id_b64} (will block until key arrives)")
            return (None, [key_id_b64])
        else:
            log.warning(f"crypto._unwrap_common({namespace_name}) key not found for id={key_id_b64} (dropping for DoS protection)")
            return (None, [])

    # Extract the encrypted portion (after the id)
    id_length = len(id_bytes)
    encrypted_data = wrapped_blob[id_length:]

    # Determine if symmetric or asymmetric based on key_data type
    key_type = key_data.get('type') if isinstance(key_data, dict) else None
    log.debug(f"crypto._unwrap_common({namespace_name}) key_type={key_type}, encrypted_data_size={len(encrypted_data)}B")

    # Decrypt/unseal
    try:
        if key_type == 'symmetric':
            # Symmetric decryption: extract nonce + ciphertext
            nonce = encrypted_data[:NONCE_SIZE]
            ciphertext = encrypted_data[NONCE_SIZE:]
            plaintext = decrypt(ciphertext, key_data['key'], nonce)
            log.debug(f"crypto._unwrap_common({namespace_name}) symmetric decrypt SUCCESS, plaintext_size={len(plaintext)}B")
        elif key_type == 'asymmetric':
            # Asymmetric unsealing
            private_key = key_data['private_key']
            plaintext = unseal(encrypted_data, private_key)
            log.debug(f"crypto._unwrap_common({namespace_name}) asymmetric unseal SUCCESS, plaintext_size={len(plaintext)}B")
        else:
            log.error(f"crypto._unwrap_common({namespace_name}) unknown key type: {key_type}")
            return (None, [])
    except Exception as e:
        log.error(f"crypto._unwrap_common({namespace_name}) decryption FAILED for id={b64encode(id_bytes)}: {e}")
        return (None, [])

    # Return decrypted bytes (caller handles JSON parsing if needed)
    return (plaintext, [])


def unwrap_transit(wrapped_blob: bytes, recorded_by: str, db: Any) -> tuple[bytes | None, list[str]]:
    """Unwrap transit-layer blob (network transport). Never blocks on missing keys.

    Only checks transit key namespace: transit_keys, connection_prekeys.
    Does NOT check: group_keys, group_prekeys.

    Args:
        wrapped_blob: Encrypted blob to unwrap
        recorded_by: Peer ID attempting to unwrap (for access control)
        db: Database connection

    Returns tuple of (plaintext, missing_key_ids).
    - If plaintext JSON: (plaintext_bytes, [])
    - If successful decrypt: (plaintext_bytes, [])
    - If key missing: (None, []) - never blocks for DoS protection
    - If other error: (None, [])
    """
    return _unwrap_common(wrapped_blob, recorded_by, db,
                         get_transit_key_by_id, block_on_missing=False,
                         namespace_name="transit")


def unwrap_event(wrapped_blob: bytes, recorded_by: str, db: Any,
                 key_cache: dict[str, dict[str, Any]] | None = None) -> tuple[bytes | None, list[str]]:
    """Unwrap event-layer blob (application data). Always blocks on missing keys.

    Only checks event key namespace: group_keys, group_prekeys.
    Does NOT check: transit_keys, connection_prekeys.

    Args:
        wrapped_blob: Encrypted blob to unwrap
        recorded_by: Peer ID attempting to unwrap (for access control)
        db: Database connection

    Returns tuple of (plaintext, missing_key_ids).
    - If plaintext JSON: (plaintext_bytes, [])
    - If successful decrypt: (plaintext_bytes, [])
    - If key missing: (None, [key_id]) - blocks for convergence guarantee
    - If other error: (None, [])
    """
    from . import wire_format
    try:
        if len(wrapped_blob) == wire_format.WIRE_SIZE:
            header, payload, _signature = wire_format.parse_envelope(wrapped_blob)
            if header.flags & wire_format.FLAG_ENCRYPTED:
                decryptors = {
                    wire_format.TYPE_MESSAGE: wire_format._decrypt_message_payload,
                    wire_format.TYPE_CHANNEL: wire_format._decrypt_channel_payload,
                    wire_format.TYPE_MESSAGE_UPDATE: wire_format._decrypt_message_update_payload,
                    wire_format.TYPE_MESSAGE_DELETION: wire_format._decrypt_message_deletion_payload,
                    wire_format.TYPE_MESSAGE_REACTION: wire_format._decrypt_message_reaction_payload,
                    wire_format.TYPE_MESSAGE_REACTION_DELETION: wire_format._decrypt_message_reaction_deletion_payload,
                    wire_format.TYPE_MESSAGE_ATTACHMENT: wire_format._decrypt_message_attachment_payload,
                    wire_format.TYPE_CHANNEL_UPDATE: wire_format._decrypt_channel_update_payload,
                    wire_format.TYPE_GROUP: wire_format._decrypt_group_payload,
                    wire_format.TYPE_GROUP_MEMBER: wire_format._decrypt_group_member_payload,
                    wire_format.TYPE_GROUP_KEY_SHARED: wire_format._decrypt_group_key_shared_payload,
                    wire_format.TYPE_USERNAME_UPDATE: wire_format._decrypt_username_update_payload,
                    wire_format.TYPE_NETWORK_NAME_UPDATE: wire_format._decrypt_network_name_update_payload,
                    wire_format.TYPE_PEER_NAME_UPDATE: wire_format._decrypt_peer_name_update_payload,
                }
                decryptor = decryptors.get(header.event_type)
                if decryptor:
                    key_id = payload[:KEY_ID_SIZE]
                    key_data = get_event_key_by_id(key_id, recorded_by, db)
                    if not key_data:
                        return None, [b64encode(key_id)]
                    try:
                        plaintext = decryptor(payload, key_data)
                        return plaintext, []
                    except Exception:
                        return None, []
    except Exception:
        pass

    return _unwrap_common(wrapped_blob, recorded_by, db,
                         get_event_key_by_id, block_on_missing=True,
                         namespace_name="event",
                         key_cache=key_cache)


def unwrap(wrapped_blob: bytes, recorded_by: str, db: Any) -> tuple[bytes | None, list[str]]:
    """DEPRECATED: Use unwrap_transit() or unwrap_event() instead.

    Extract key id from blob, determine if sym or asym, fetch key, then unseal or decrypt.

    Args:
        wrapped_blob: Encrypted blob to unwrap
        recorded_by: Peer ID attempting to unwrap (for access control)
        db: Database connection

    Returns tuple of (plaintext, missing_key_ids).
    - If plaintext JSON: (plaintext_bytes, [])
    - If successful decrypt: (plaintext_bytes, [])
    - If key missing: (None, [key_id_hex])
    - If other error: (None, [])

    Blob remains in store for future retry/recovery.
    """
    return _unwrap_common(wrapped_blob, recorded_by, db,
                         get_key_by_id, block_on_missing=True,
                         namespace_name="both")


def wrap(plaintext_bytes: bytes, key_data: Any, db: Any) -> bytes:
    """Deterministically wrap plaintext bytes with key. Same plaintext + key → same blob.

    Args:
        plaintext_bytes: Raw bytes to encrypt (caller must canonicalize JSON if needed)
        key_data: Key dict with 'id', 'type', and 'key' or 'public_key'
        db: Database connection

    Returns:
        Encrypted blob: id + encrypted_data (nonce + ciphertext for symmetric)

    Note: This function ONLY handles encryption. Caller must handle JSON canonicalization.
    For double-wrapping, simply call wrap() again on the output bytes.
    """
    import logging
    log = logging.getLogger(__name__)

    # Get the key id from key_data
    id_bytes = key_data['id'] if isinstance(key_data, dict) else b''
    key_id_b64 = b64encode(id_bytes)

    # Determine if symmetric or asymmetric based on key_data type
    key_type = key_data.get('type') if isinstance(key_data, dict) else None

    log.warning(f"[CRYPTO_WRAP] key_id={key_id_b64[:20]}..., key_type={key_type}, id_bytes_len={len(id_bytes)}")
    log.info(f"crypto.wrap() wrapping with key_id={key_id_b64}, key_type={key_type}, id_bytes_len={len(id_bytes)}")
    log.debug(f"crypto.wrap() called: key_id={key_id_b64}, key_type={key_type}, plaintext_size={len(plaintext_bytes)}B")

    if key_type == 'symmetric':
        # Symmetric encryption with deterministic nonce
        nonce = deterministic_nonce(id_bytes, plaintext_bytes)
        ciphertext = encrypt(plaintext_bytes, key_data['key'], nonce)
        encrypted_data = nonce + ciphertext
        log.debug(f"crypto.wrap() symmetric encrypt SUCCESS, encrypted_size={len(encrypted_data)}B")
    elif key_type == 'asymmetric':
        # Asymmetric sealing with deterministic ephemeral keypair (content-addressed compatible)
        public_key = key_data['public_key']
        encrypted_data = seal(plaintext_bytes, public_key)
        log.debug(f"crypto.wrap() asymmetric seal SUCCESS, encrypted_size={len(encrypted_data)}B")
    else:
        log.error(f"crypto.wrap() unknown key type: {key_type}")
        raise ValueError(f"Unknown key type: {key_type}")

    # Return id + encrypted data
    result = id_bytes + encrypted_data
    log.debug(f"crypto.wrap() returning blob size={len(result)}B")
    return result


# ===== File Slice Encryption =====

def encrypt_file_slice(plaintext: bytes, key: bytes, nonce: bytes) -> tuple[bytes, bytes]:
    """Encrypt file slice with XChaCha20-Poly1305. Returns (ciphertext, tag).

    Args:
        plaintext: Plain bytes to encrypt (max 450 bytes)
        key: 32-byte symmetric encryption key
        nonce: 24-byte nonce

    Returns:
        (ciphertext, poly_tag) where ciphertext is plaintext-sized, tag is 16 bytes
    """
    import logging
    log = logging.getLogger(__name__)

    box = nacl.secret.SecretBox(key)
    encrypted = box.encrypt(plaintext, nonce)
    # PyNaCl prepends nonce, we return just ciphertext + tag
    ciphertext_and_tag = encrypted[NONCE_SIZE:]  # Skip the nonce prefix

    # Split into ciphertext and tag
    ciphertext = ciphertext_and_tag[:-16]  # All but last 16 bytes
    poly_tag = ciphertext_and_tag[-16:]  # Last 16 bytes

    log.debug(f"encrypt_file_slice() encrypted {len(plaintext)}B → ciphertext {len(ciphertext)}B + tag {len(poly_tag)}B")
    return (ciphertext, poly_tag)


def decrypt_file_slice(ciphertext: bytes, poly_tag: bytes, key: bytes, nonce: bytes) -> bytes:
    """Decrypt file slice with XChaCha20-Poly1305.

    Args:
        ciphertext: Ciphertext bytes
        poly_tag: 16-byte AEAD authentication tag
        key: 32-byte symmetric decryption key
        nonce: 24-byte nonce

    Returns:
        Plaintext bytes

    Raises:
        nacl.exceptions.CryptoError: If verification fails
    """
    import logging
    log = logging.getLogger(__name__)

    box = nacl.secret.SecretBox(key)
    # PyNaCl expects nonce + ciphertext + tag
    encrypted_with_nonce = nonce + ciphertext + poly_tag
    plaintext = box.decrypt(encrypted_with_nonce)

    log.debug(f"decrypt_file_slice() decrypted {len(ciphertext)}B ciphertext → {len(plaintext)}B plaintext")
    return plaintext


def compute_file_id(full_ciphertext: bytes) -> str:
    """Compute file ID as BLAKE2b-128 hash of complete ciphertext stream.

    Args:
        full_ciphertext: Complete ciphertext (all slices concatenated)

    Returns:
        Base64-encoded 16-byte hash
    """
    file_id_bytes = hash(full_ciphertext, size=16)
    return b64encode(file_id_bytes)


def compute_root_hash(slice_ciphertexts: list[bytes]) -> bytes:
    """Compute root hash as BLAKE2b-256 over concatenated slice ciphertexts.

    Args:
        slice_ciphertexts: List of ciphertext bytes for each slice

    Returns:
        32-byte BLAKE2b-256 hash
    """
    concatenated = b''.join(slice_ciphertexts)
    return hash(concatenated, size=32)


def derive_slice_nonce(nonce_prefix: bytes, slice_number: int) -> bytes:
    """Derive slice-specific nonce from 20-byte prefix and slice number.

    Construct: nonce_prefix (20 bytes) || slice_number (4 bytes, little-endian)

    Args:
        nonce_prefix: 20-byte nonce prefix
        slice_number: Slice index (0-based)

    Returns:
        24-byte nonce for this slice
    """
    slice_no_bytes = slice_number.to_bytes(4, byteorder='little')
    return nonce_prefix + slice_no_bytes


# ===== Incremental Hash Builder =====

class HashBuilder:
    """Incremental BLAKE2b hash builder for streaming hash computation.

    Uses nacl.hashlib.blake2b which supports .update() for streaming.
    Produces identical results to compute_file_id() and compute_root_hash()
    when fed the same concatenated data.

    Example:
        # Streaming root_hash computation (equivalent to compute_root_hash)
        builder = HashBuilder(size=32)
        for ciphertext in slice_ciphertexts:
            builder.update(ciphertext)
        root_hash = builder.digest()

        # Streaming file_id computation (equivalent to compute_file_id)
        builder = HashBuilder(size=16)
        for ciphertext in slice_ciphertexts:
            builder.update(ciphertext)
        file_id = b64encode(builder.digest())
    """

    def __init__(self, size: int = 32):
        """Initialize hash builder.

        Args:
            size: Digest size in bytes (16 for file_id, 32 for root_hash)
        """
        from nacl import hashlib as nacl_hashlib
        self._hasher = nacl_hashlib.blake2b(digest_size=size)

    def update(self, data: bytes) -> None:
        """Add data to the hash computation.

        Args:
            data: Bytes to add (e.g., a slice ciphertext)
        """
        self._hasher.update(data)

    def digest(self) -> bytes:
        """Return the current hash digest.

        Returns:
            Hash bytes (length determined by size parameter)
        """
        return self._hasher.digest()
