"""Invite accepted event type (local-only, captures invite acceptance)."""
from typing import Any
import json
import logging
import crypto
import store
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(invite_id: str, invite_private_key: bytes,
           invite_prekey_shared_id: str,
           invite_transit_key: bytes, invite_transit_key_id: str,
           invite_group_key: bytes, invite_group_key_id: str,
           network_key: bytes, network_key_id: str,
           peer_id: str, t_ms: int, db: Any) -> str:
    """Create local invite_accepted event (not shareable).

    This event captures the invite acceptance action and stores ALL
    out-of-band data from the invite link for event-sourcing (reprojection).

    Args:
        invite_id: The invite event being accepted
        invite_private_key: Private key for invite proof signature (stored in prekeys)
        invite_prekey_shared_id: Prekey_shared event ID (synthetic ID for invite prekey)
        invite_transit_key: Outer encryption key for Bob's bootstrap events → Alice
        invite_transit_key_id: Pre-computed ID for invite_transit_key (from Alice)
        invite_group_key: Inner encryption key for Bob's bootstrap events
        invite_group_key_id: Pre-computed ID for invite_group_key (from Alice)
        network_key: Long-term group symmetric key
        network_key_id: Pre-computed ID for network_key (from Alice)
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
        'invite_prekey_shared_id': invite_prekey_shared_id,
        'invite_transit_key': crypto.b64encode(invite_transit_key),
        'invite_transit_key_id': invite_transit_key_id,  # Alice's pre-computed ID
        'invite_group_key': crypto.b64encode(invite_group_key),
        'invite_group_key_id': invite_group_key_id,  # Alice's pre-computed ID
        'network_key': crypto.b64encode(network_key),
        'network_key_id': network_key_id,  # Alice's pre-computed ID
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

    This restores raw keys (invite_group_key, network_key) from the invite link
    and enables full reprojection without the original invite link.
    """
    log.info(f"invite_accepted.project() id={invite_accepted_id}, recorded_by={recorded_by}")

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
    invite_prekey_shared_id = event_data['invite_prekey_shared_id']

    # Store invite_private_key in prekeys table (detached prekey)
    # Used for invite proof signature only (not for decryption)
    unsafedb.execute(
        """INSERT OR IGNORE INTO prekeys
           (prekey_id, owner_peer_id, public_key, private_key, prekey_shared_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            invite_prekey_shared_id,  # Use prekey_shared_id as prekey_id (detached)
            recorded_by,
            invite_public_key,
            invite_private_key,
            invite_prekey_shared_id,
            event_data['created_at']
        )
    )

    log.info(f"invite_accepted.project() stored invite_private_key in prekeys table")

    # Restore invite_transit_key (outer encryption for Bob→Alice bootstrap events)
    # Alice created a key event with this ID, Bob/others need to restore it with the same ID
    if 'invite_transit_key' in event_data and 'invite_transit_key_id' in event_data:
        invite_transit_key = crypto.b64decode(event_data['invite_transit_key'])
        invite_transit_key_id = event_data['invite_transit_key_id']  # Alice's event ID

        log.info(f"invite_accepted.project() restoring invite_transit_key with key_id={invite_transit_key_id}, recorded_by={recorded_by[:20]}...")

        # Store directly in keys table with Alice's event ID
        unsafedb.execute(
            "INSERT OR REPLACE INTO keys (key_id, key, created_at) VALUES (?, ?, ?)",
            (invite_transit_key_id, invite_transit_key, event_data['created_at'])
        )

        # Track key ownership
        unsafedb.execute(
            "INSERT OR IGNORE INTO key_ownership (key_id, peer_id, created_at) VALUES (?, ?, ?)",
            (invite_transit_key_id, recorded_by, event_data['created_at'])
        )

        # Verify insertion worked
        verify = unsafedb.query_one("SELECT 1 FROM keys WHERE key_id = ?", (invite_transit_key_id,))
        log.info(f"invite_accepted.project() invite_transit_key insertion verified: {verify is not None}")

    # Restore invite_group_key (inner encryption for bootstrap events)
    # Alice created a key event with this ID, Bob needs to restore it with the same ID
    if 'invite_group_key' in event_data and 'invite_group_key_id' in event_data:
        invite_group_key = crypto.b64decode(event_data['invite_group_key'])
        invite_group_key_id = event_data['invite_group_key_id']  # Alice's event ID

        log.info(f"invite_accepted.project() restoring invite_group_key with key_id={invite_group_key_id}, recorded_by={recorded_by[:20]}...")

        # Store directly in keys table with Alice's event ID
        # (Bob doesn't create a new key event, he uses Alice's ID)
        unsafedb.execute(
            "INSERT OR REPLACE INTO keys (key_id, key, created_at) VALUES (?, ?, ?)",
            (invite_group_key_id, invite_group_key, event_data['created_at'])
        )

        # Track key ownership
        unsafedb.execute(
            "INSERT OR IGNORE INTO key_ownership (key_id, peer_id, created_at) VALUES (?, ?, ?)",
            (invite_group_key_id, recorded_by, event_data['created_at'])
        )

        # Verify insertion worked
        verify = unsafedb.query_one("SELECT 1 FROM keys WHERE key_id = ?", (invite_group_key_id,))
        log.info(f"invite_accepted.project() invite_group_key insertion verified: {verify is not None}")

    # Restore network_key (long-term group key)
    # Alice created a key event with this ID, Bob needs to restore it with the same ID
    if 'network_key' in event_data and 'network_key_id' in event_data:
        network_key = crypto.b64decode(event_data['network_key'])
        network_key_id = event_data['network_key_id']  # Alice's event ID

        log.info(f"invite_accepted.project() restoring network_key with key_id={network_key_id}, recorded_by={recorded_by[:20]}...")

        # Store directly in keys table with Alice's event ID
        # (Bob doesn't create a new key event, he uses Alice's ID)
        unsafedb.execute(
            "INSERT OR REPLACE INTO keys (key_id, key, created_at) VALUES (?, ?, ?)",
            (network_key_id, network_key, event_data['created_at'])
        )

        # Track key ownership
        unsafedb.execute(
            "INSERT OR IGNORE INTO key_ownership (key_id, peer_id, created_at) VALUES (?, ?, ?)",
            (network_key_id, recorded_by, event_data['created_at'])
        )

        # Verify insertion worked
        verify = unsafedb.query_one("SELECT 1 FROM keys WHERE key_id = ?", (network_key_id,))
        log.info(f"invite_accepted.project() network_key insertion verified: {verify is not None}")

    # Track in invite_acceptances table for audit/debugging (simplified - no invite_group_key_shared_id)
    safedb.execute(
        """INSERT OR IGNORE INTO invite_acceptances
           (invite_accepted_id, invite_id, invite_group_key_shared_id, peer_id, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            invite_accepted_id,
            invite_id,
            '',  # No longer using invite_group_key_shared_id
            recorded_by,
            event_data['created_at']
        )
    )

    # Mark as valid
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (invite_accepted_id, recorded_by)
    )

    log.info(f"invite_accepted.project() completed for {recorded_by}")
