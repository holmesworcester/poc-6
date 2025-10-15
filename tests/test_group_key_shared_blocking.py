#!/usr/bin/env python3
"""Test that group_key_shared events wrapped to other peers are blocked (not skipped)."""
import sys
import sqlite3
from db import Database
import schema
import crypto
import store
from events.identity import user
from events.group import group, group_key, group_key_shared
from events.transit import recorded

def test_group_key_shared_blocking():
    """Test that Alice is BLOCKED (not SKIPPED) when trying to project group_key_shared wrapped to Bob."""

    # Setup
    conn = sqlite3.Connection(':memory:')
    db = Database(conn)
    schema.create_all(db)

    # Create Alice
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    alice_peer_id = alice['peer_id']
    alice_peer_shared_id = alice['peer_shared_id']

    # Create Bob
    bob = user.new_network(name='Bob', t_ms=2000, db=db)
    bob_peer_id = bob['peer_id']
    bob_peer_shared_id = bob['peer_shared_id']

    print(f"Alice: {alice_peer_id[:20]}...")
    print(f"Bob: {bob_peer_id[:20]}...")

    # Alice needs to have Bob's transit_prekey_shared in her database
    # (In real usage, this would happen via sync)
    # First Alice needs Bob's peer_shared event (dependency for transit_prekey_shared)
    recorded_id_for_bob_peer_shared = recorded.create(bob_peer_shared_id, alice_peer_id, 2400, db, return_dupes=True)
    recorded.project(recorded_id_for_bob_peer_shared, db)
    # Then Alice records Bob's transit_prekey_shared
    bob_transit_prekey_shared_id = bob['transit_prekey_shared_id']
    recorded_id_for_bob_prekey = recorded.create(bob_transit_prekey_shared_id, alice_peer_id, 2500, db, return_dupes=True)
    recorded.project(recorded_id_for_bob_prekey, db)

    # Alice creates a group key
    key_id = group_key.create(
        peer_id=alice_peer_id,
        t_ms=3000,
        db=db
    )
    print(f"Created key: {key_id[:20]}...")

    # Alice creates group_key_shared wrapped to Bob's prekey
    key_shared_id = group_key_shared.create(
        key_id=key_id,
        peer_id=alice_peer_id,
        peer_shared_id=alice_peer_shared_id,
        recipient_peer_id=bob_peer_shared_id,  # Use peer_shared_id, not peer_id
        t_ms=4000,
        db=db
    )
    print(f"Created key_shared: {key_shared_id[:20]}...")

    # Check that key_shared event was created and stored
    key_shared_blob = store.get(key_shared_id, db)
    print(f"key_shared blob size: {len(key_shared_blob)} bytes")

    # After create(), the event was automatically projected via store.event()
    # Since the inner event is wrapped to Bob's prekey, Alice should be BLOCKED

    # Check if blocked
    blocked = db.query_one(
        "SELECT * FROM blocked_events_ephemeral WHERE recorded_by = ?",
        (alice_peer_id,)
    )

    if blocked:
        print(f"\n✓ CORRECT: Alice is BLOCKED")
        print(f"  recorded_id: {blocked['recorded_id'][:20] if isinstance(blocked['recorded_id'], str) else crypto.b64encode(blocked['recorded_id'])[:20]}...")
        print(f"  missing_deps: {blocked['missing_deps']}")
        return True
    else:
        print(f"\n✗ WRONG: Alice is NOT blocked")

        # Check if event was marked as valid
        valid = db.query_one(
            "SELECT * FROM valid_events WHERE event_id = ? AND recorded_by = ?",
            (key_shared_id, alice_peer_id)
        )
        if valid:
            print(f"  Event was marked as VALID (should not be!)")
        else:
            print(f"  Event was SKIPPED (should be BLOCKED instead!)")

        return False

if __name__ == '__main__':
    success = test_group_key_shared_blocking()
    sys.exit(0 if success else 1)
