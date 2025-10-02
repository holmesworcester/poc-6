"""Crypto functions for wrapping and unwrapping."""
from typing import Any, Tuple
import json

import nacl.secret
import nacl.hash
import nacl.signing
import nacl.encoding
import nacl.utils
from nacl.public import SealedBox

from events import key, network
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


def canonicalize_json(obj: dict[str, Any]) -> bytes:
    """Canonicalize a JSON object to bytes for deterministic encryption."""
    return json.dumps(obj, sort_keys=True, separators=(',', ':')).encode('utf-8')


def sign_event(event_data: dict[str, Any], private_key: bytes) -> dict[str, Any]:
    """Add signature to event dict. Signature is computed over all fields except itself."""
    canonical = canonicalize_json(event_data)
    sig = sign(canonical, private_key)
    return {**event_data, 'signature': sig.hex()}


def verify_event(event_data: dict[str, Any], public_key: bytes) -> bool:
    """Verify event signature. Returns False if signature missing or invalid."""
    sig_hex = event_data.get('signature')
    if not sig_hex:
        return False

    # Remove signature from dict for verification
    event_without_sig = {k: v for k, v in event_data.items() if k != 'signature'}
    canonical = canonicalize_json(event_without_sig)

    try:
        return verify(canonical, bytes.fromhex(sig_hex), public_key)
    except Exception:
        return False


def deterministic_nonce(hint: bytes, plaintext_bytes: bytes) -> bytes:
    """Derive a deterministic nonce from hint and canonical plaintext.

    This makes encryption deterministic: same plaintext + key → same ciphertext.
    Enables content-addressing and deduplication.
    """
    return hash(hint + plaintext_bytes, size=NONCE_SIZE)


# ===== Wrap/Unwrap Functions =====


def unwrap_and_store(blob: bytes, t_ms: int, db: Any) -> str:
    """Unwrap an incoming transit blob and store it, with its corresponding "first_seen" event blob.

    The seen_by_peer_id is determined by the transit_key (the receiving peer).
    Returns empty string if unwrap fails (missing key, decryption error, etc.).
    """
    import logging
    log = logging.getLogger(__name__)

    hint = key.extract_hint(blob)
    # For incoming blobs: seen_by_peer_id = peer who owns the transit_key (the receiving peer)
    seen_by_peer_id = network.get_peer_id_for_transit_key(hint, db)

    unwrapped_blob = unwrap(blob, db)
    if unwrapped_blob is None:
        log.info(f"Skipping storage for blob with hint {hint.hex()}: unwrap failed")
        return ""

    first_seen_id = store.store_with_first_seen(unwrapped_blob, seen_by_peer_id, t_ms, db)
    return first_seen_id

def unwrap(wrapped_blob: bytes, db: Any) -> bytes | None:
    """Extract key hint from blob, determine if sym or asym, fetch key, then unseal or decrypt.

    Returns None on non-critical errors (missing key, decryption failure, invalid JSON, non-canonical).
    Logs errors for debugging. Blob remains in store for future retry/recovery.
    """
    import logging
    log = logging.getLogger(__name__)

    # Extract hint from blob
    try:
        hint = key.extract_hint(wrapped_blob)
    except Exception as e:
        log.error(f"Failed to extract hint from blob: {e}")
        return None

    # Get the key using the hint
    key_data = key.get_key(hint, db)
    if not key_data:
        log.warning(f"Key not found for hint: {hint.hex()} (may arrive later)")
        return None

    # Extract the encrypted portion (after the hint)
    hint_length = len(hint)
    encrypted_data = wrapped_blob[hint_length:]

    # Determine if symmetric or asymmetric based on key_data type
    key_type = key_data.get('type') if isinstance(key_data, dict) else None

    # Decrypt/unseal
    try:
        if key_type == 'symmetric':
            # Symmetric decryption: extract nonce + ciphertext
            nonce = encrypted_data[:NONCE_SIZE]
            ciphertext = encrypted_data[NONCE_SIZE:]
            plaintext = decrypt(ciphertext, key_data['key'], nonce)
        elif key_type == 'asymmetric':
            # Asymmetric unsealing
            private_key = key_data['private_key']
            plaintext = unseal(encrypted_data, private_key)
        else:
            log.error(f"Unknown key type: {key_type}")
            return None
    except Exception as e:
        log.error(f"Decryption failed for hint {hint.hex()}: {e}")
        return None

    # Parse and verify JSON is canonical
    try:
        event_data = json.loads(plaintext.decode('utf-8'))
    except Exception as e:
        log.error(f"Invalid JSON after decryption: {e}")
        return None

    # Verify canonicalization
    canonical_check = canonicalize_json(event_data)
    if canonical_check != plaintext:
        log.error(f"Non-canonical JSON detected in blob (hint: {hint.hex()})")
        return None

    return plaintext


def wrap(plaintext: dict[str, Any], key_data: Any, db: Any) -> bytes:
    """Deterministically wrap plaintext with key. Same plaintext + key → same blob."""
    # Canonicalize plaintext for deterministic encryption
    canonical_bytes = canonicalize_json(plaintext)

    # Get the key hint from key_data
    hint = key_data['hint'] if isinstance(key_data, dict) else b''

    # Determine if symmetric or asymmetric based on key_data type
    key_type = key_data.get('type') if isinstance(key_data, dict) else None

    if key_type == 'symmetric':
        # Symmetric encryption with deterministic nonce
        nonce = deterministic_nonce(hint, canonical_bytes)
        ciphertext = encrypt(canonical_bytes, key_data['key'], nonce)
        encrypted_data = nonce + ciphertext
    elif key_type == 'asymmetric':
        # Asymmetric sealing (inherently non-deterministic, but used for one-time messages)
        public_key = key_data['public_key']
        encrypted_data = seal(canonical_bytes, public_key)
    else:
        raise ValueError(f"Unknown key type: {key_type}")

    # Return hint + encrypted data
    return hint + encrypted_data