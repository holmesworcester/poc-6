"""
Scenario test: Distributed bootstrap - cascade via sync.

Tests that device linking works correctly in a distributed scenario where phone
and laptop eventually sync to exchange events. The projection model blocks
until dependencies are valid (delivered via sync), ensuring proper causal ordering.
"""
import sqlite3
from unittest import mock
from core.db import Database
from core import schema, store
from events.identity import user, invite, peer_shared, peer
from tests.utils import tick_helper


def test_device_link_without_remote_invite_blob(fresh_db):
    """Device linking blocks events (cascade-via-sync) when invite blob is not in local store.

    This simulates the distributed case where:
    - Phone creates the peer invite (stored in phone's store)
    - Laptop accepts the invite but cannot access phone's store
    - Laptop's peer_shared is BLOCKED until invite blob arrives via sync

    The cascade flow:
    1. invite_accepted stores invite_private_key locally
    2. peer_shared event blocks on invite_id (waiting for sync)
    3. Bootstrap connection uses invite_accepteds (has inviter's address)
    4. When invite blob syncs and projects, cascade unblocks peer_shared
    """
    db = fresh_db

    print("\n=== Setup: Alice creates network on phone ===")

    # Alice creates a network on her phone
    alice_phone = user.new_network(name='Alice', t_ms=1000, db=db, device_name='Phone')
    db.commit()
    print(f"Alice created network on phone: peer_id={alice_phone['peer_id'][:20]}...")

    # Alice creates a peer invite on her phone for device linking
    print("\n=== Alice creates peer invite for laptop ===")

    invite_id, link_url, invite_data = invite.create(
        peer_id=alice_phone['peer_id'],
        t_ms=2000,
        db=db,
        mode='peer',
        user_id=alice_phone['user_id']
    )
    db.commit()
    print(f"Peer invite created: {invite_id[:20]}...")

    # Record the invite_id for patching
    peer_invite_id = invite_id

    # Create laptop peer
    print("\n=== Create laptop peer ===")
    alice_laptop_peer_id = peer.create(t_ms=3000, db=db)
    print(f"Created laptop peer: {alice_laptop_peer_id[:20]}...")

    # Accept the invite to get the private key
    accepted = invite.accept(alice_laptop_peer_id, link_url, t_ms=3001, db=db)
    assert accepted['mode'] == 'peer'
    print(f"Accepted peer invite")

    db.commit()

    # Now simulate the distributed case: patch store.get() to return None for
    # the peer invite blob when called during laptop's projection
    print("\n=== Link laptop with patched store (simulating distributed isolation) ===")

    # Save original store.get
    original_store_get = store.get

    def patched_store_get(blob_id, db_conn):
        """Return None for the peer invite blob, simulating distributed isolation."""
        if blob_id == peer_invite_id:
            print(f"  [PATCHED] store.get({blob_id[:20]}...) -> None (simulating distributed)")
            return None
        return original_store_get(blob_id, db_conn)

    # Patch store.get for the duration of peer_shared.join()
    with mock.patch.object(store, 'get', side_effect=patched_store_get):
        # Complete the peer linking
        alice_laptop = peer_shared.join(
            peer_id=alice_laptop_peer_id,
            peer_invite_id=accepted['invite_id'],
            peer_invite_private_key=accepted['invite_private_key'],
            user_id=accepted['user_id'],
            prekey_id=accepted['invite_prekey_id'],
            t_ms=3002,
            db=db,
            device_name='Laptop',
            network_id=accepted['network_id']  # Required for trust anchoring
        )

    db.commit()
    print(f"Laptop join() returned (events stored)")
    print(f"  peer_id={alice_laptop['peer_id'][:20]}...")
    print(f"  user_id={alice_laptop['user_id'][:20]}...")

    # Verify both devices have the same user_id (from invite_accepteds, not peer_shared)
    assert alice_laptop['user_id'] == alice_phone['user_id'], \
        "Both devices should have same user_id"
    print(f"OK: Both devices share user_id: {alice_laptop['user_id'][:20]}...")

    # With cascade-via-sync, peer_shared should be BLOCKED (not projected) until invite arrives
    laptop_peer_shared = db.query_one(
        "SELECT * FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ?",
        (alice_laptop['peer_shared_id'], alice_laptop['peer_id'])
    )
    # peer_shared should NOT be projected yet - it's blocked waiting for invite_id
    assert laptop_peer_shared is None, "peer_shared should be BLOCKED (not projected) until invite syncs"
    print(f"OK: peer_shared is blocked (waiting for invite_id via sync)")

    # Verify laptop has blocked events waiting for invite sync
    blocked_count = db.query_one(
        "SELECT COUNT(*) as cnt FROM blocked_events_ephemeral WHERE recorded_by = ?",
        (alice_laptop['peer_id'],)
    )
    assert blocked_count['cnt'] > 0, "Laptop should have blocked events waiting for invite sync"
    print(f"OK: Laptop has {blocked_count['cnt']} blocked events (waiting for invite_id via sync)")

    # Verify invite_accepteds was created (needed for bootstrap connection)
    ia_row = db.query_one(
        "SELECT * FROM invite_accepteds WHERE invite_id = ? AND recorded_by = ?",
        (peer_invite_id, alice_laptop['peer_id'])
    )
    assert ia_row is not None, "invite_accepted should be stored for bootstrap connection"
    assert ia_row['user_id'] == alice_phone['user_id'], "invite_accepted should have user_id"
    print(f"OK: invite_accepteds has connection info for bootstrap")

    print(f"\nPASSED: Device link correctly blocks until invite syncs (cascade-via-sync)!")


def test_user_join_without_remote_invite_blob(fresh_db):
    """User joining blocks events (cascade-via-sync) when invite blob is not in local store.

    This simulates the distributed case where:
    - Phone creates network and user invite (stored in phone's store)
    - Bob's laptop accepts the invite but cannot access phone's store
    - Bob's events are BLOCKED until invite blob arrives via sync

    The cascade flow:
    1. invite_accepted marks network_id valid (trust anchor)
    2. Bob's user event blocks on invite_id (waiting for sync)
    3. Bootstrap connection uses invite_accepteds (has inviter's address)
    4. When invite blob syncs and projects, cascade unblocks user -> peer_shared
    """
    db = fresh_db

    print("\n=== Setup: Alice creates network on phone ===")

    # Alice creates a network
    alice = user.new_network(name='Alice', t_ms=1000, db=db, device_name='Phone')
    db.commit()
    print(f"Alice created network: user_id={alice['user_id'][:20]}...")

    # Alice creates a user invite for Bob
    print("\n=== Alice creates user invite for Bob ===")

    invite_id, link_url, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=2000,
        db=db,
        mode='user'
    )
    db.commit()
    print(f"User invite created: {invite_id[:20]}...")

    # Record the invite_id for patching
    user_invite_id = invite_id

    # Create Bob's peer
    print("\n=== Create Bob's peer ===")
    bob_peer_id = peer.create(t_ms=3000, db=db)
    print(f"Created Bob's peer: {bob_peer_id[:20]}...")

    db.commit()

    # Simulate distributed case: patch store.get() to return None for the invite blob
    print("\n=== Bob joins with patched store (simulating distributed isolation) ===")

    original_store_get = store.get

    def patched_store_get(blob_id, db_conn):
        """Return None for the user invite blob, simulating distributed isolation."""
        if blob_id == user_invite_id:
            print(f"  [PATCHED] store.get({blob_id[:20]}...) -> None (simulating distributed)")
            return None
        return original_store_get(blob_id, db_conn)

    with mock.patch.object(store, 'get', side_effect=patched_store_get):
        # Bob joins the network (user.join takes the link_url string)
        bob = user.join(
            peer_id=bob_peer_id,
            invite_link=link_url,
            name='Bob',
            t_ms=3002,
            db=db,
            device_name='Laptop'
        )

    db.commit()
    print(f"Bob join() returned (events stored)")
    print(f"  user_id={bob['user_id'][:20]}...")
    print(f"  peer_shared_id={bob['peer_shared_id'][:20]}...")

    # With cascade-via-sync, Bob's user should be BLOCKED (not projected) until invite arrives
    bob_user = db.query_one(
        "SELECT * FROM users WHERE user_id = ? AND recorded_by = ?",
        (bob['user_id'], bob['peer_id'])
    )
    # User should NOT be projected yet - it's blocked waiting for invite_id
    assert bob_user is None, "Bob's user should be BLOCKED (not projected) until invite syncs"
    print(f"OK: Bob's user is blocked (waiting for invite_id via sync)")

    # Verify Bob has blocked events (user and peer_shared waiting for invite_id via sync)
    blocked_count = db.query_one(
        "SELECT COUNT(*) as cnt FROM blocked_events_ephemeral WHERE recorded_by = ?",
        (bob['peer_id'],)
    )
    assert blocked_count['cnt'] > 0, "Bob should have blocked events waiting for invite sync"
    print(f"OK: Bob has {blocked_count['cnt']} blocked events (waiting for invite_id via sync)")

    # Verify invite_accepteds was created (needed for bootstrap connection)
    ia_row = db.query_one(
        "SELECT * FROM invite_accepteds WHERE invite_id = ? AND recorded_by = ?",
        (user_invite_id, bob['peer_id'])
    )
    assert ia_row is not None, "invite_accepted should be stored for bootstrap connection"
    assert ia_row['network_id'] == alice['network_id'], "invite_accepted should have network_id"
    print(f"OK: invite_accepteds has connection info for bootstrap")

    print(f"\nPASSED: User join correctly blocks until invite syncs (cascade-via-sync)!")


def test_invite_accepteds_stores_user_id_for_device_link(fresh_db):
    """Verify invite_accepteds stores user_id correctly for device linking fallback."""
    db = fresh_db

    print("\n=== Setup: Create network and device link invite ===")

    alice = user.new_network(name='Alice', t_ms=1000, db=db, device_name='Phone')
    db.commit()

    invite_id, link_url, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=2000,
        db=db,
        mode='peer',
        user_id=alice['user_id']
    )
    db.commit()
    print(f"Peer invite for device link: {invite_id[:20]}...")

    # Create and accept on laptop
    laptop_peer_id = peer.create(t_ms=3000, db=db)
    accepted = invite.accept(laptop_peer_id, link_url, t_ms=3001, db=db)

    # Join with the invite
    laptop = peer_shared.join(
        peer_id=laptop_peer_id,
        peer_invite_id=accepted['invite_id'],
        peer_invite_private_key=accepted['invite_private_key'],
        user_id=accepted['user_id'],
        prekey_id=accepted['invite_prekey_id'],
        t_ms=3002,
        db=db,
        device_name='Laptop',
        network_id=accepted['network_id']  # Required for trust anchoring
    )
    db.commit()

    # Verify invite_accepteds has the user_id for fallback lookups
    print("\n=== Verify invite_accepteds table ===")

    ia_row = db.query_one(
        "SELECT * FROM invite_accepteds WHERE invite_id = ? AND recorded_by = ?",
        (invite_id, laptop_peer_id)
    )
    assert ia_row is not None, "invite_accepted should be stored"
    assert ia_row['invite_private_key'] is not None, "invite_private_key should be stored"
    assert ia_row['user_id'] == alice['user_id'], \
        f"user_id should be stored for device link fallback, got {ia_row['user_id']}"
    print(f"OK: invite_accepteds has user_id={ia_row['user_id'][:20]}...")
    print(f"OK: invite_accepteds has invite_private_key (for deriving pubkey)")

    print(f"\nPASSED: invite_accepteds stores user_id correctly!")
