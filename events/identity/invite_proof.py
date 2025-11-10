"""invite_proof event type for proving invite acceptance.

This module handles invite proof validation, which proves that a joiner
has the invite private key. The proof is now separate from user/link events,
making invite validation consistent across all join/link scenarios.

Replaces the embedded invite_signature fields in user and link events.
"""
from typing import Any
import logging
import crypto
import store
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(invite_id: str, mode: str, joiner_peer_shared_id: str,
           user_id: str | None, link_user_id: str | None,
           invite_private_key: bytes, peer_id: str, t_ms: int, db: Any) -> str:
    """Create invite_proof event proving invite acceptance.

    Args:
        invite_id: The invite being accepted
        mode: 'user' or 'link'
        joiner_peer_shared_id: Joiner's public peer identity
        user_id: User ID (for mode='user')
        link_user_id: Target user ID (for mode='link')
        invite_private_key: Invite private key for signing
        peer_id: Local peer creating this proof
        t_ms: Timestamp
        db: Database connection

    Returns:
        invite_proof_id: The created event ID
    """
    # Validate mode-specific fields
    if mode == 'user':
        if not user_id:
            raise ValueError("mode='user' requires user_id")
        if link_user_id:
            raise ValueError("mode='user' cannot have link_user_id")
    elif mode == 'link':
        if not link_user_id:
            raise ValueError("mode='link' requires link_user_id")
        if user_id:
            raise ValueError("mode='link' cannot have user_id")
    else:
        raise ValueError(f"Invalid mode: {mode}")

    # Derive public key from private key
    import nacl.signing
    signing_key = nacl.signing.SigningKey(invite_private_key)
    invite_public_key = bytes(signing_key.verify_key)
    invite_pubkey_b64 = crypto.b64encode(invite_public_key)

    # Create signature message based on mode
    if mode == 'user':
        # Sign: joiner_peer_shared_id + ":" + invite_id + ":" + user_id
        proof_message = f"{joiner_peer_shared_id}:{invite_id}:{user_id}".encode('utf-8')
    else:  # mode == 'link'
        # Sign: joiner_peer_shared_id + ":" + invite_id + ":" + link_user_id
        proof_message = f"{joiner_peer_shared_id}:{invite_id}:{link_user_id}".encode('utf-8')

    invite_signature = crypto.sign(proof_message, invite_private_key)
    invite_signature_b64 = crypto.b64encode(invite_signature)

    # Build event data
    event_data = {
        'type': 'invite_proof',
        'invite_id': invite_id,
        'mode': mode,
        'joiner_peer_shared_id': joiner_peer_shared_id,
        'invite_pubkey': invite_pubkey_b64,
        'invite_signature': invite_signature_b64,
        'created_at': t_ms
    }

    # Add mode-specific fields
    if mode == 'user':
        event_data['user_id'] = user_id
    else:
        event_data['link_user_id'] = link_user_id

    # Sign with peer's private key
    from events.identity import peer
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Store as signed plaintext
    blob = crypto.canonicalize_json(signed_event)
    unsafedb = create_unsafe_db(db)
    invite_proof_id = store.blob(blob, t_ms, return_dupes=True, unsafedb=unsafedb)

    # Create recorded event
    from events.transit import recorded
    recorded_id = recorded.create(invite_proof_id, peer_id, t_ms, db, return_dupes=True)

    # Project immediately (local event)
    recorded.project_ids([recorded_id], db)

    log.info(f"invite_proof.create() created {invite_proof_id[:20]}... mode={mode} for peer {peer_id[:20]}...")

    return invite_proof_id


def project(invite_proof_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project invite_proof event: validate and apply to tables.

    Args:
        invite_proof_id: The invite_proof event ID
        recorded_by: Local peer who recorded this
        recorded_at: When recorded
        db: Database connection

    Returns:
        invite_proof_id if successful, None if validation fails
    """
    log.info(f"invite_proof.project() event_id={invite_proof_id[:20]}... recorded_by={recorded_by[:20]}...")

    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    blob = store.get(invite_proof_id, unsafedb)
    if not blob:
        log.warning(f"invite_proof.project() blob not found")
        return None

    event_data = crypto.parse_json(blob)

    # Verify signature by peer
    try:
        crypto.verify_signed_by_peer_shared(event_data, recorded_by, db)
    except Exception as e:
        log.warning(f"invite_proof.project() peer signature verification failed: {e}")
        return None

    # Extract fields
    invite_id = event_data['invite_id']
    mode = event_data['mode']
    joiner_peer_shared_id = event_data['joiner_peer_shared_id']
    invite_pubkey_b64 = event_data['invite_pubkey']
    invite_signature_b64 = event_data['invite_signature']

    # Get mode-specific IDs
    user_id = event_data.get('user_id')
    link_user_id = event_data.get('link_user_id')

    # Validate mode-specific fields
    if mode == 'user' and not user_id:
        log.warning(f"invite_proof.project() mode='user' missing user_id")
        return None
    if mode == 'link' and not link_user_id:
        log.warning(f"invite_proof.project() mode='link' missing link_user_id")
        return None

    # Get invite event to verify
    invite_blob = store.get(invite_id, unsafedb)
    if not invite_blob:
        log.warning(f"invite_proof.project() invite not found: {invite_id}")
        return None

    invite_data = crypto.parse_json(invite_blob)

    # Verify invite_pubkey matches invite event
    if invite_pubkey_b64 != invite_data.get('invite_pubkey'):
        log.warning(f"invite_proof.project() invite_pubkey mismatch")
        return None

    # Verify mode matches invite (if invite has mode field)
    invite_mode = invite_data.get('mode', 'user')  # Default to 'user' for backward compatibility
    if mode != invite_mode:
        log.warning(f"invite_proof.project() mode mismatch: proof={mode}, invite={invite_mode}")
        return None

    # Verify the Ed25519 signature
    if mode == 'user':
        proof_message = f"{joiner_peer_shared_id}:{invite_id}:{user_id}".encode('utf-8')
    else:
        proof_message = f"{joiner_peer_shared_id}:{invite_id}:{link_user_id}".encode('utf-8')

    invite_pubkey = crypto.b64decode(invite_pubkey_b64)
    invite_signature = crypto.b64decode(invite_signature_b64)

    if not crypto.verify(proof_message, invite_signature, invite_pubkey):
        log.warning(f"invite_proof.project() signature verification failed")
        return None

    # Store in invite_proofs table
    safedb.execute("""
        INSERT OR IGNORE INTO invite_proofs
        (invite_proof_id, invite_id, mode, joiner_peer_shared_id,
         user_id, link_user_id, created_at, recorded_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        invite_proof_id,
        invite_id,
        mode,
        joiner_peer_shared_id,
        user_id,
        link_user_id,
        event_data['created_at'],
        recorded_by
    ))

    # Apply effects based on mode
    if mode == 'user':
        # Add to group_members (user joining network)
        group_id = invite_data['group_id']
        safedb.execute("""
            INSERT OR IGNORE INTO group_members
            (member_id, group_id, user_id, added_by, created_at, recorded_by, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,  # Use user_id as member_id for invite joins
            group_id,
            user_id,
            joiner_peer_shared_id,  # Self-added via invite
            event_data['created_at'],
            recorded_by,
            recorded_at
        ))
        log.info(f"invite_proof.project() added user {user_id[:20]}... to group {group_id[:20]}...")

    else:  # mode == 'link'
        # Link peer to user (device linking)
        safedb.execute("""
            INSERT OR IGNORE INTO linked_peers
            (user_id, peer_shared_id, recorded_by)
            VALUES (?, ?, ?)
        """, (link_user_id, joiner_peer_shared_id, recorded_by))
        log.info(f"invite_proof.project() linked peer {joiner_peer_shared_id[:20]}... to user {link_user_id[:20]}...")

    log.info(f"invite_proof.project() completed for {recorded_by[:20]}...")
    return invite_proof_id
