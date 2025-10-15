"""Invite accepted event type (local-only, captures invite acceptance)."""
from typing import Any
import json
import logging
import crypto
import store
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(invite_id: str, invite_private_key: bytes,
           invite_transit_prekey_shared_id: str,
           invite_transit_key: bytes, invite_transit_key_id: str,
           peer_id: str, t_ms: int, db: Any) -> str:
    """Create local invite_accepted event (not shareable).

    This event captures the invite acceptance action and stores ALL
    out-of-band data from the invite link for event-sourcing (reprojection).

    Args:
        invite_id: The invite event being accepted
        invite_private_key: Private key for invite proof signature (stored in prekeys)
        invite_transit_prekey_shared_id: Prekey_shared event ID (synthetic ID for invite prekey)
        invite_transit_key: Outer encryption key for Bob's bootstrap events → Alice
        invite_transit_key_id: Pre-computed ID for invite_transit_key (from Alice)
        peer_id: Bob's peer_id (local)
        t_ms: Timestamp
        db: Database connection

    Returns:
        invite_accepted_id: Event ID
    """
    log.info(f"invite_accepted.create() for invite={invite_id}, peer={peer_id}")

    event_data = {
        'type': 'invite_accepted',
        'invite_id': invite_id,
        'invite_private_key': crypto.b64encode(invite_private_key),
        'invite_transit_prekey_shared_id': invite_transit_prekey_shared_id,
        'invite_transit_key': crypto.b64encode(invite_transit_key),
        'invite_transit_key_id': invite_transit_key_id,  # Alice's pre-computed ID
        'created_by': peer_id,
        'created_at': t_ms
    }

    blob = json.dumps(event_data).encode()

    # Store with recorded wrapper and projection
    invite_accepted_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"invite_accepted.create() created invite_accepted_id={invite_accepted_id}")
    return invite_accepted_id


def project(invite_accepted_id: str, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Project invite_accepted: restore ALL invite link data for event-sourcing.

    This restores the invite_transit_key from the invite link and enables
    full reprojection without the original invite link.
    """
    log.warning(f"[INVITE_ACCEPTED_PROJECT_ENTRY] id={invite_accepted_id[:20]}..., recorded_by={recorded_by[:20]}...")

    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    blob = store.get(invite_accepted_id, unsafedb)
    if not blob:
        log.warning(f"invite_accepted.project() blob not found")
        return

    event_data = crypto.parse_json(blob)

    # Get invite event to extract public key
    invite_id = event_data['invite_id']
    invite_blob = store.get(invite_id, unsafedb)
    if not invite_blob:
        log.warning(f"invite_accepted.project() invite blob not found: {invite_id}")
        return

    invite_event = crypto.parse_json(invite_blob)

    # Extract keys
    invite_private_key = crypto.b64decode(event_data['invite_private_key'])
    invite_public_key = crypto.b64decode(invite_event['invite_prekey_public_key'])
    invite_transit_prekey_shared_id = event_data['invite_transit_prekey_shared_id']

    # Store invite_private_key in transit_prekeys table (detached prekey)
    # Used for invite proof signature only (not for decryption)
    unsafedb.execute(
        """INSERT OR IGNORE INTO transit_prekeys
           (prekey_id, owner_peer_id, public_key, private_key, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            invite_transit_prekey_shared_id,  # Use prekey_shared_id as prekey_id (detached)
            recorded_by,
            invite_public_key,
            invite_private_key,
            event_data['created_at']
        )
    )

    log.warning(f"[INVITE_ACCEPTED_PROJECT] stored invite_private_key prekey_id={invite_transit_prekey_shared_id[:20]}... for peer {recorded_by[:20]}...")

    # Restore invite_transit_key (outer encryption for Bob→Alice bootstrap events)
    # Alice created a key event with this ID, Bob/others need to restore it with the same ID
    if 'invite_transit_key' in event_data and 'invite_transit_key_id' in event_data:
        invite_transit_key = crypto.b64decode(event_data['invite_transit_key'])
        invite_transit_key_id = event_data['invite_transit_key_id']  # Alice's event ID

        log.info(f"invite_accepted.project() restoring invite_transit_key with key_id={invite_transit_key_id}, recorded_by={recorded_by[:20]}...")

        # Store directly in transit_keys table with Alice's event ID
        # Use INSERT OR IGNORE to preserve Alice's original entry (owner_peer_id=Alice)
        # Bob doesn't need this key in transit_keys since he's the sender, not receiver
        unsafedb.execute(
            "INSERT OR IGNORE INTO transit_keys (key_id, key, owner_peer_id, created_at) VALUES (?, ?, ?, ?)",
            (invite_transit_key_id, invite_transit_key, recorded_by, event_data['created_at'])
        )

        # Verify insertion worked
        verify = unsafedb.query_one("SELECT 1 FROM transit_keys WHERE key_id = ?", (invite_transit_key_id,))
        log.info(f"invite_accepted.project() invite_transit_key insertion verified: {verify is not None}")

    # Mark invite_accepted as valid
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (invite_accepted_id, recorded_by)
    )

    # Mark the invite itself as valid (restores out-of-band trust from invite link)
    # This is necessary for reprojection since the invite link is not available
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (invite_id, recorded_by)
    )

    log.info(f"invite_accepted.project() completed for {recorded_by}")
