"""Admin event type (shareable, plaintext) - grants admin status to a user.

This is a first-class event type, NOT a group. Admin status is granted by:
- Bootstrap: signed_by=network_id (verified with network_pubkey)
- Ongoing: signed_by=peer_shared_id (verified with peer.pubkey + admin_grant chain)
"""
from typing import Any
import logging
import crypto
import store
from db import create_safe_db

log = logging.getLogger(__name__)


def create(
    user_id: str,
    network_id: str,
    signed_by: str,
    signer_private_key: bytes,
    t_ms: int,
    peer_id: str,
    db: Any,
    admin_grant: str | None = None
) -> str:
    """Create an admin event granting admin status to a user.

    Args:
        user_id: The user being granted admin
        network_id: The network this admin grant is for
        signed_by: Either network_id (bootstrap) or peer_shared_id (ongoing)
        signer_private_key: Private key corresponding to signed_by
        t_ms: Timestamp
        peer_id: Local peer ID (for recording)
        db: Database connection
        admin_grant: Prior admin_id for authorization chain (None for bootstrap)

    Returns:
        admin_id: The ID of the created admin event
    """
    event_data = {
        'type': 'admin',
        'user_id': user_id,
        'network_id': network_id,
        'signed_by': signed_by,
        'created_at': t_ms,
    }

    if admin_grant:
        event_data['admin_grant'] = admin_grant

    # Sign the event
    signed_event = crypto.sign_event(event_data, signer_private_key)

    # Store as signed plaintext (no encryption)
    blob = crypto.canonicalize_json(signed_event)

    # Store event with recorded wrapper and projection
    admin_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"admin.create() created admin grant: admin_id={admin_id[:20]}..., "
             f"user_id={user_id[:20]}..., network_id={network_id[:20]}..., "
             f"signed_by={signed_by[:20]}...")

    return admin_id


def project(admin_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project admin event into admins table.

    Validates:
    - Bootstrap (signed_by == network_id): Verify signature with network_pubkey
    - Ongoing (signed_by == peer_shared_id): Verify with peer.pubkey + check admin_grant chain

    Args:
        admin_id: The admin event ID
        recorded_by: The peer projecting this event
        recorded_at: Timestamp of recording
        db: Database connection

    Returns:
        admin_id if projection succeeded, None if dependencies not ready
    """
    log.warning(f"[ADMIN_PROJECT_ENTRY] admin.project() called: admin_id={admin_id[:20]}..., "
                f"recorded_by={recorded_by[:20]}...")

    # Get blob from store
    blob = store.get(admin_id, db)
    if not blob:
        log.warning(f"[ADMIN_PROJECT_EARLY_RETURN] Blob not found for admin_id={admin_id[:20]}...")
        return None

    # Parse JSON (signed plaintext, no decryption needed)
    event_data = crypto.parse_json(blob)

    # Validate event type
    if event_data.get('type') != 'admin':
        log.warning(f"[ADMIN_PROJECT_EARLY_RETURN] Invalid event type: {event_data.get('type')}")
        return None

    signed_by = event_data['signed_by']
    network_id = event_data['network_id']
    user_id = event_data['user_id']
    admin_grant = event_data.get('admin_grant')

    # Determine if this is bootstrap (signed_by == network_id) or ongoing
    is_bootstrap = (signed_by == network_id)

    if is_bootstrap:
        # Bootstrap: verify signature with network_pubkey
        from events.identity import network
        try:
            network_pubkey = network.get_public_key(network_id, recorded_by, db)
        except (ValueError, KeyError):
            log.warning(f"[ADMIN_PROJECT_EARLY_RETURN] Network {network_id[:20]}... not available yet")
            return None

        if not crypto.verify_event(event_data, network_pubkey):
            log.warning(f"[ADMIN_PROJECT_EARLY_RETURN] Bootstrap signature verification failed")
            return None

        log.info(f"[ADMIN_PROJECT] Bootstrap admin grant verified for user {user_id[:20]}...")

    else:
        # Ongoing: verify signature with peer_shared.pubkey
        from events.identity import peer_shared
        try:
            signer_pubkey = peer_shared.get_public_key(signed_by, recorded_by, db)
        except ValueError:
            log.warning(f"[ADMIN_PROJECT_EARLY_RETURN] peer_shared {signed_by[:20]}... not available yet")
            return None

        if not crypto.verify_event(event_data, signer_pubkey):
            log.warning(f"[ADMIN_PROJECT_EARLY_RETURN] Ongoing signature verification failed")
            return None

        # Validate admin_grant chain
        if not admin_grant:
            log.warning(f"[ADMIN_PROJECT_EARLY_RETURN] Ongoing admin grant requires admin_grant reference")
            return None

        # Check that signer's user is admin via admin_grant
        safedb = create_safe_db(db, recorded_by=recorded_by)

        # Get signer's user_id
        signer_user_row = safedb.query_one(
            "SELECT user_id FROM users WHERE peer_id = ? AND recorded_by = ?",
            (signed_by, recorded_by)
        )
        if not signer_user_row:
            log.warning(f"[ADMIN_PROJECT_EARLY_RETURN] Signer user not found for {signed_by[:20]}...")
            return None

        signer_user_id = signer_user_row['user_id']

        # Verify admin_grant references an admin event for signer's user
        grant_row = safedb.query_one(
            "SELECT user_id FROM admins WHERE admin_id = ? AND recorded_by = ?",
            (admin_grant, recorded_by)
        )
        if not grant_row or grant_row['user_id'] != signer_user_id:
            log.warning(f"[ADMIN_PROJECT_EARLY_RETURN] admin_grant {admin_grant[:20]}... "
                        f"does not authorize signer {signer_user_id[:20]}...")
            return None

        log.info(f"[ADMIN_PROJECT] Ongoing admin grant verified for user {user_id[:20]}... "
                 f"authorized by {signer_user_id[:20]}...")

    # Insert into admins table
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute(
        """INSERT OR IGNORE INTO admins
           (admin_id, network_id, user_id, signed_by, admin_grant, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            admin_id,
            network_id,
            user_id,
            signed_by,
            admin_grant,
            event_data['created_at'],
            recorded_by,
            recorded_at
        )
    )

    log.warning(f"[ADMIN_PROJECT_SUCCESS] Admin grant inserted: admin_id={admin_id[:20]}..., "
                f"user_id={user_id[:20]}...")

    # Notify events waiting for this user's admin status
    # user_id is the user event ID, but waiters block on peer_shared_id
    # Look up the peer_shared_id for this user
    user_row = safedb.query_one(
        "SELECT peer_id FROM users WHERE user_id = ? AND recorded_by = ?",
        (user_id, recorded_by)
    )
    if user_row:
        peer_shared_id = user_row['peer_id']  # users.peer_id stores peer_shared_id
        import queues
        dep_key = f"admin_status_{peer_shared_id}"
        unblocked = queues.blocked.notify_event_valid(dep_key, recorded_by, safedb)
        if unblocked:
            log.info(f"[ADMIN_PROJECT] Unblocked {len(unblocked)} events waiting for admin status of {peer_shared_id[:20]}...")
            # Re-project unblocked events
            from events.network import recorded as recorded_module
            recorded_module.project_ids(unblocked, db)

    return admin_id


def is_user_admin(user_id: str, network_id: str, recorded_by: str, db: Any) -> bool:
    """Check if a user has admin status in a network.

    Args:
        user_id: The user to check
        network_id: The network to check admin status for
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        True if user is an admin, False otherwise
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    admin_row = safedb.query_one(
        "SELECT 1 FROM admins WHERE user_id = ? AND network_id = ? AND recorded_by = ?",
        (user_id, network_id, recorded_by)
    )

    return admin_row is not None


def my_grant(user_id: str, network_id: str, recorded_by: str, db: Any) -> str | None:
    """Get the admin_id that granted admin to a user.

    Used for creating admin_grant chain when granting admin to others.

    Args:
        user_id: The admin user
        network_id: The network
        recorded_by: Peer perspective
        db: Database connection

    Returns:
        admin_id if found, None otherwise
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    admin_row = safedb.query_one(
        "SELECT admin_id FROM admins WHERE user_id = ? AND network_id = ? AND recorded_by = ?",
        (user_id, network_id, recorded_by)
    )

    return admin_row['admin_id'] if admin_row else None
