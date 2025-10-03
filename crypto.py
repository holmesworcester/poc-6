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
from nacl.public import SealedBox

from events import key
import store

# ===== Constants =====

HINT_SIZE = 16  # bytes (128 bits)
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


def hash(data: bytes, size: int = HINT_SIZE) -> bytes:
    """BLAKE2b hash. Default 16 bytes (128 bits) for IDs and hints."""
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
    """Seal a message to a public key (anonymous sender)."""
    # Convert Ed25519 to X25519 for encryption
    verify_key = nacl.signing.VerifyKey(public_key)
    public = verify_key.to_curve25519_public_key()
    return SealedBox(public).encrypt(plaintext)


def unseal(ciphertext: bytes, private_key: bytes) -> bytes:
    """Unseal a message with a private key."""
    # Convert Ed25519 to X25519 for encryption
    signing_key = nacl.signing.SigningKey(private_key)
    private = signing_key.to_curve25519_private_key()
    return SealedBox(private).decrypt(ciphertext)


def b64encode(data: bytes) -> str:
    """Encode bytes to base64 ASCII string."""
    return base64.b64encode(data).decode('ascii')


def b64decode(data: str) -> bytes:
    """Decode base64 ASCII string to bytes."""
    return base64.b64decode(data)


def canonicalize_json(obj: dict[str, Any]) -> bytes:
    """Canonicalize a JSON object to bytes for deterministic encryption."""
    return json.dumps(obj, sort_keys=True, separators=(',', ':')).encode('utf-8')


def sign_event(event_data: dict[str, Any], private_key: bytes) -> dict[str, Any]:
    """Add signature to event dict. Signature is computed over all fields except itself."""
    import logging
    log = logging.getLogger(__name__)

    event_type = event_data.get('type', 'unknown')
    log.debug(f"crypto.sign_event() signing event type={event_type}")

    canonical = canonicalize_json(event_data)
    sig = sign(canonical, private_key)
    result = {**event_data, 'signature': b64encode(sig)}

    log.debug(f"crypto.sign_event() signed event type={event_type}, canonical_size={len(canonical)}B")
    return result


def verify_event(event_data: dict[str, Any], public_key: bytes) -> bool:
    """Verify event signature. Returns False if signature missing or invalid."""
    import logging
    log = logging.getLogger(__name__)

    event_type = event_data.get('type', 'unknown')
    sig_b64 = event_data.get('signature')

    if not sig_b64:
        log.warning(f"crypto.verify_event() missing signature for event type={event_type}")
        return False

    # Remove signature from dict for verification
    event_without_sig = {k: v for k, v in event_data.items() if k != 'signature'}
    canonical = canonicalize_json(event_without_sig)

    try:
        result = verify(canonical, b64decode(sig_b64), public_key)
        if result:
            log.debug(f"crypto.verify_event() signature valid for event type={event_type}")
        else:
            log.warning(f"crypto.verify_event() signature INVALID for event type={event_type}")
        return result
    except Exception as e:
        log.error(f"crypto.verify_event() verification failed for event type={event_type}: {e}")
        return False


def deterministic_nonce(hint: bytes, plaintext_bytes: bytes) -> bytes:
    """Derive a deterministic nonce from hint and canonical plaintext.

    This makes encryption deterministic: same plaintext + key → same ciphertext.
    Enables content-addressing and deduplication.
    """
    return hash(hint + plaintext_bytes, size=NONCE_SIZE)


# ===== Wrap/Unwrap Functions =====


def unwrap(wrapped_blob: bytes, recorded_by: str, db: Any) -> tuple[bytes | None, list[str]]:
    """Extract key id from blob, determine if sym or asym, fetch key, then unseal or decrypt.

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
    import logging
    log = logging.getLogger(__name__)

    log.debug(f"crypto.unwrap() called with blob size={len(wrapped_blob)}B, recorded_by={recorded_by[:20]}...")

    # Check if blob is plaintext JSON (starts with '{' or '[')
    if wrapped_blob and wrapped_blob[:1] in (b'{', b'['):
        try:
            # Verify it's valid JSON
            json.loads(wrapped_blob.decode('utf-8'))
            log.debug(f"crypto.unwrap() blob is plaintext JSON, returning as-is")
            return (wrapped_blob, [])
        except Exception:
            # Not valid JSON, continue with decrypt attempt
            pass

    # Extract id from blob
    try:
        id_bytes = key.extract_id(wrapped_blob)
        key_id_b64 = b64encode(id_bytes)
        log.debug(f"crypto.unwrap() extracted key_id={key_id_b64}")
    except Exception as e:
        log.error(f"crypto.unwrap() failed to extract id from blob: {e}")
        return (None, [])

    # Get the key using the id (filtered by recorded_by for access control)
    key_data = key.get_key_by_id(id_bytes, recorded_by, db)
    if not key_data:
        key_id_b64 = b64encode(id_bytes)
        log.warning(f"crypto.unwrap() key not found for id={key_id_b64} (will block until key arrives)")
        return (None, [key_id_b64])

    # Extract the encrypted portion (after the id)
    id_length = len(id_bytes)
    encrypted_data = wrapped_blob[id_length:]

    # Determine if symmetric or asymmetric based on key_data type
    key_type = key_data.get('type') if isinstance(key_data, dict) else None
    log.debug(f"crypto.unwrap() key_type={key_type}, encrypted_data_size={len(encrypted_data)}B")

    # Decrypt/unseal
    try:
        if key_type == 'symmetric':
            # Symmetric decryption: extract nonce + ciphertext
            nonce = encrypted_data[:NONCE_SIZE]
            ciphertext = encrypted_data[NONCE_SIZE:]
            plaintext = decrypt(ciphertext, key_data['key'], nonce)
            log.debug(f"crypto.unwrap() symmetric decrypt SUCCESS, plaintext_size={len(plaintext)}B")
        elif key_type == 'asymmetric':
            # Asymmetric unsealing
            private_key = key_data['private_key']
            plaintext = unseal(encrypted_data, private_key)
            log.debug(f"crypto.unwrap() asymmetric unseal SUCCESS, plaintext_size={len(plaintext)}B")
        else:
            log.error(f"crypto.unwrap() unknown key type: {key_type}")
            return (None, [])
    except Exception as e:
        log.error(f"crypto.unwrap() decryption FAILED for id={b64encode(id_bytes)}: {e}")
        return (None, [])

    # Return decrypted bytes (caller handles JSON parsing if needed)
    return (plaintext, [])


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

    log.debug(f"crypto.wrap() called: key_id={key_id_b64}, key_type={key_type}, plaintext_size={len(plaintext_bytes)}B")

    if key_type == 'symmetric':
        # Symmetric encryption with deterministic nonce
        nonce = deterministic_nonce(id_bytes, plaintext_bytes)
        ciphertext = encrypt(plaintext_bytes, key_data['key'], nonce)
        encrypted_data = nonce + ciphertext
        log.debug(f"crypto.wrap() symmetric encrypt SUCCESS, encrypted_size={len(encrypted_data)}B")
    elif key_type == 'asymmetric':
        # Asymmetric sealing (inherently non-deterministic, but used for one-time messages)
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