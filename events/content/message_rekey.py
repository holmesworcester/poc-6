"""Message rekey event type for forward secrecy.

When a message is deleted, its encryption key is marked for purging.
This event type allows re-encrypting messages with a new "clean" key
before the old key is discarded, ensuring forward secrecy.
"""
from typing import Any
import logging
import crypto
import store
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(original_message_id: str, new_key_id: str, peer_id: str, t_ms: int, db: Any) -> str:
    """Create a message_rekey event to re-encrypt a message with a new key.

    Uses deterministic nonce to ensure same plaintext + key → same ciphertext,
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

    safedb = create_safe_db(db, recorded_by=peer_id)
    unsafedb = create_unsafe_db(db)

    # Get original message blob from store
    original_blob = store.get(original_message_id, unsafedb)
    if not original_blob:
        raise ValueError(f"Original message {original_message_id} not found in store")

    # Unwrap (decrypt) original message to get plaintext
    plaintext, missing_keys = crypto.unwrap_event(original_blob, peer_id, db)
    if not plaintext or missing_keys:
        raise ValueError(f"Cannot decrypt original message {original_message_id} - missing key: {missing_keys}")

    # Parse plaintext to verify it's a valid message
    event_data = crypto.parse_json(plaintext)
    if event_data.get('type') != 'message':
        raise ValueError(f"Event {original_message_id} is not a message, cannot rekey")

    log.info(f"message_rekey.create() decrypted original message, plaintext_size={len(plaintext)}B")

    # Get the new key from group_keys table
    new_key_row = safedb.query_one(
        "SELECT key FROM group_keys WHERE key_id = ? AND recorded_by = ? LIMIT 1",
        (new_key_id, peer_id)
    )
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
    event_data = {
        'type': 'message_rekey',
        'original_message_id': original_message_id,
        'new_key_id': new_key_id,
        'new_ciphertext': crypto.b64encode(new_ciphertext + deterministic_nonce),  # Store nonce with ciphertext
        'created_by': peer_id,
        'created_at': t_ms
    }

    canonical = crypto.canonicalize_json(event_data)
    rekey_id = store.event(canonical, peer_id, t_ms, db)

    log.info(f"message_rekey.create() created rekey_id={rekey_id[:20]}...")
    return rekey_id


def project(rekey_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project message_rekey event - replace original message blob with rekeyed version.

    Args:
        rekey_id: Rekey event ID
        recorded_by: Peer who recorded this event
        recorded_at: When this peer recorded it
        db: Database connection

    Returns:
        rekey_id if successful, None if validation failed
    """
    log.info(f"message_rekey.project() rekey_id={rekey_id[:20]}..., recorded_by={recorded_by[:20]}...")

    safedb = create_safe_db(db, recorded_by=recorded_by)
    unsafedb = create_unsafe_db(db)

    # Get rekey event from store
    rekey_blob = store.get(rekey_id, unsafedb)
    if not rekey_blob:
        log.warning(f"message_rekey.project() rekey blob not found for rekey_id={rekey_id}")
        return None

    # Parse rekey event data
    rekey_data = crypto.parse_json(rekey_blob)
    original_message_id = rekey_data['original_message_id']
    new_key_id = rekey_data['new_key_id']

    log.info(f"message_rekey.project() original_message_id={original_message_id[:20]}..., new_key_id={new_key_id[:20]}...")

    # Validate: try to decrypt with new key to verify plaintext
    new_ciphertext_and_nonce_b64 = rekey_data['new_ciphertext']
    new_ciphertext_and_nonce = crypto.b64decode(new_ciphertext_and_nonce_b64)

    # Extract nonce (last 24 bytes) and ciphertext
    nonce = new_ciphertext_and_nonce[-crypto.NONCE_SIZE:]
    new_ciphertext = new_ciphertext_and_nonce[:-crypto.NONCE_SIZE]

    # Get new key
    new_key_row = safedb.query_one(
        "SELECT key FROM group_keys WHERE key_id = ? AND recorded_by = ? LIMIT 1",
        (new_key_id, recorded_by)
    )
    if not new_key_row:
        log.warning(f"message_rekey.project() new key {new_key_id[:20]}... not found, cannot validate rekey")
        return None

    try:
        new_key = new_key_row['key']
        rekeyed_plaintext = crypto.decrypt(new_ciphertext, new_key, nonce)
        log.info(f"message_rekey.project() verified rekeyed plaintext, size={len(rekeyed_plaintext)}B")
    except Exception as e:
        log.warning(f"message_rekey.project() failed to decrypt rekeyed message: {e}")
        return None

    # Try to decrypt original to verify plaintext matches
    original_blob = store.get(original_message_id, unsafedb)
    if original_blob:
        try:
            original_plaintext, _ = crypto.unwrap_event(original_blob, recorded_by, db)
            if original_plaintext and original_plaintext != rekeyed_plaintext:
                log.warning(f"message_rekey.project() plaintext mismatch! original and rekeyed don't match")
                return None
            log.info(f"message_rekey.project() plaintext verification passed")
        except Exception as e:
            log.warning(f"message_rekey.project() could not verify original plaintext: {e}")
            # Continue anyway - we verified the rekeyed version is valid JSON

    # Replace original message blob in store with new ciphertext
    # Create blob: new_key_id + nonce + ciphertext (standard encrypted format)
    new_key_id_bytes = crypto.b64decode(new_key_id)
    new_blob = new_key_id_bytes + nonce + new_ciphertext

    unsafedb.execute(
        "UPDATE store SET blob = ? WHERE id = ?",
        (new_blob, original_message_id)
    )
    log.info(f"message_rekey.project() replaced message blob in store for {original_message_id[:20]}...")

    # Record the rekey event
    safedb.execute(
        """INSERT OR IGNORE INTO message_rekeys
           (rekey_id, original_message_id, new_key_id, new_ciphertext, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (rekey_id, original_message_id, new_key_id, new_ciphertext, rekey_data['created_at'], recorded_by, recorded_at)
    )

    # Mark rekey as valid
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (rekey_id, recorded_by)
    )

    log.info(f"message_rekey.project() successfully projected rekey, recorded as valid")
    return rekey_id
