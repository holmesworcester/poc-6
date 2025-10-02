"""
Scenario test: Three players with message transit via sync.

Alice and Bob can communicate (have each other's prekeys).
Charlie is isolated (no prekeys registered).

Tests:
- Alice and Bob exchange sync requests and receive each other's messages
- Charlie's sync requests fail (no prekeys)
- Charlie only sees their own messages
"""
import sqlite3
import pytest
from db import Database
import schema
from events import peer, key, group, channel, message, sync
import crypto
import store


def test_three_player_messaging():
    """Three peers: Alice and Bob can sync, Charlie is isolated."""

    # Setup: Initialize in-memory database
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Create three peers
    alice_peer_id, alice_peer_shared_id = peer.create(t_ms=1000, db=db)
    bob_peer_id, bob_peer_shared_id = peer.create(t_ms=2000, db=db)
    charlie_peer_id, charlie_peer_shared_id = peer.create(t_ms=3000, db=db)

    # Register prekeys in shared test database
    # Get public keys from peers table
    alice_public_key = peer.get_public_key(alice_peer_id, db)
    bob_public_key = peer.get_public_key(bob_peer_id, db)

    # Register prekeys for Alice and Bob (all peers can use them in this shared test db)
    db.execute(
        "INSERT INTO pre_keys (peer_id, public_key, created_at) VALUES (?, ?, ?)",
        (bob_peer_id, bob_public_key, 4000)
    )
    db.execute(
        "INSERT INTO pre_keys (peer_id, public_key, created_at) VALUES (?, ?, ?)",
        (alice_peer_id, alice_public_key, 5000)
    )

    # NOTE: Charlie can send sync requests (has prekeys), but Alice/Bob should reject them
    # because they don't recognize Charlie's peer_shared_id

    db.commit()

    # Pre-share peer_shared events between Alice and Bob (favorable initial conditions)
    # Alice receives Bob's peer_shared
    from events import first_seen
    bob_peer_shared_blob = store.get(bob_peer_shared_id, db)
    alice_sees_bob_peer_shared = store.blob(bob_peer_shared_blob, 5500, True, db)
    bob_peer_shared_fs_id = first_seen.create(alice_sees_bob_peer_shared, alice_peer_id, 5500, db, True)
    first_seen.project(bob_peer_shared_fs_id, db)

    # Bob receives Alice's peer_shared
    alice_peer_shared_blob = store.get(alice_peer_shared_id, db)
    bob_sees_alice_peer_shared = store.blob(alice_peer_shared_blob, 5600, True, db)
    alice_peer_shared_fs_id = first_seen.create(bob_sees_alice_peer_shared, bob_peer_id, 5600, db, True)
    first_seen.project(alice_peer_shared_fs_id, db)

    db.commit()

    # Each peer creates their own group, channel setup (like single-player test)
    # Alice's setup
    alice_key_id = key.create(alice_peer_id, t_ms=6000, db=db)

    # Pre-share Alice's key with Bob
    alice_key_blob = store.get(alice_key_id, db)
    bob_sees_alice_key = store.blob(alice_key_blob, 6500, True, db)
    alice_key_fs_id = first_seen.create(bob_sees_alice_key, bob_peer_id, 6500, db, True)
    first_seen.project(alice_key_fs_id, db)

    alice_group_id = group.create(
        name='Alice Group',
        peer_id=alice_peer_id,
        peer_shared_id=alice_peer_shared_id,
        key_id=alice_key_id,
        t_ms=7000,
        db=db
    )
    alice_channel_id = channel.create(
        name='general',
        group_id=alice_group_id,
        peer_id=alice_peer_id,
        peer_shared_id=alice_peer_shared_id,
        key_id=alice_key_id,
        t_ms=8000,
        db=db
    )

    # Bob's setup
    bob_key_id = key.create(bob_peer_id, t_ms=9000, db=db)

    # Pre-share Bob's key with Alice
    bob_key_blob = store.get(bob_key_id, db)
    alice_sees_bob_key = store.blob(bob_key_blob, 9500, True, db)
    bob_key_fs_id = first_seen.create(alice_sees_bob_key, alice_peer_id, 9500, db, True)
    first_seen.project(bob_key_fs_id, db)

    bob_group_id = group.create(
        name='Bob Group',
        peer_id=bob_peer_id,
        peer_shared_id=bob_peer_shared_id,
        key_id=bob_key_id,
        t_ms=10000,
        db=db
    )
    bob_channel_id = channel.create(
        name='general',
        group_id=bob_group_id,
        peer_id=bob_peer_id,
        peer_shared_id=bob_peer_shared_id,
        key_id=bob_key_id,
        t_ms=11000,
        db=db
    )

    # Charlie's setup
    charlie_key_id = key.create(charlie_peer_id, t_ms=12000, db=db)
    charlie_group_id = group.create(
        name='Charlie Group',
        peer_id=charlie_peer_id,
        peer_shared_id=charlie_peer_shared_id,
        key_id=charlie_key_id,
        t_ms=13000,
        db=db
    )
    charlie_channel_id = channel.create(
        name='general',
        group_id=charlie_group_id,
        peer_id=charlie_peer_id,
        peer_shared_id=charlie_peer_shared_id,
        key_id=charlie_key_id,
        t_ms=14000,
        db=db
    )

    # Each peer creates a message
    alice_msg = message.create_message(
        params={
            'content': 'Hello from Alice',
            'channel_id': alice_channel_id,
            'group_id': alice_group_id,
            'peer_id': alice_peer_id,
            'peer_shared_id': alice_peer_shared_id,
            'key_id': alice_key_id
        },
        t_ms=15000,
        db=db
    )

    bob_msg = message.create_message(
        params={
            'content': 'Hello from Bob',
            'channel_id': bob_channel_id,
            'group_id': bob_group_id,
            'peer_id': bob_peer_id,
            'peer_shared_id': bob_peer_shared_id,
            'key_id': bob_key_id
        },
        t_ms=16000,
        db=db
    )

    charlie_msg = message.create_message(
        params={
            'content': 'Hello from Charlie',
            'channel_id': charlie_channel_id,
            'group_id': charlie_group_id,
            'peer_id': charlie_peer_id,
            'peer_shared_id': charlie_peer_shared_id,
            'key_id': charlie_key_id
        },
        t_ms=17000,
        db=db
    )

    # Round 1: Send sync requests
    sync.send_request(to_peer_id=bob_peer_id, from_peer_id=alice_peer_id, from_peer_shared_id=alice_peer_shared_id, t_ms=18000, db=db)
    sync.send_request(to_peer_id=alice_peer_id, from_peer_id=bob_peer_id, from_peer_shared_id=bob_peer_shared_id, t_ms=19000, db=db)
    sync.send_request(to_peer_id=alice_peer_id, from_peer_id=charlie_peer_id, from_peer_shared_id=charlie_peer_shared_id, t_ms=20000, db=db)
    sync.send_request(to_peer_id=bob_peer_id, from_peer_id=charlie_peer_id, from_peer_shared_id=charlie_peer_shared_id, t_ms=21000, db=db)

    # Receive sync requests - this unwraps requests and auto-sends responses
    sync.receive(batch_size=10, t_ms=22000, db=db)

    # Round 2: Receive sync responses
    sync.receive(batch_size=100, t_ms=23000, db=db)

    # Verify event visibility via valid_events table
    # Alice should have received Bob's events (peer_shared, group, channel, message)
    alice_valid_events = db.query(
        "SELECT event_id FROM valid_events WHERE seen_by_peer_id = ?",
        (alice_peer_id,)
    )
    print(f"Alice has {len(alice_valid_events)} valid events")

    # Bob should have received Alice's events
    bob_valid_events = db.query(
        "SELECT event_id FROM valid_events WHERE seen_by_peer_id = ?",
        (bob_peer_id,)
    )
    print(f"Bob has {len(bob_valid_events)} valid events")

    # Charlie should only have his own events
    charlie_valid_events = db.query(
        "SELECT event_id FROM valid_events WHERE seen_by_peer_id = ?",
        (charlie_peer_id,)
    )
    print(f"Charlie has {len(charlie_valid_events)} valid events")

    # Check specific message events in store
    alice_msg_blob = store.get(alice_msg['id'], db)
    bob_msg_blob = store.get(bob_msg['id'], db)
    charlie_msg_blob = store.get(charlie_msg['id'], db)

    # Alice should have Bob's message event
    bob_msg_valid_for_alice = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND seen_by_peer_id = ?",
        (bob_msg['id'], alice_peer_id)
    )
    print(f"Bob's message valid for Alice: {bob_msg_valid_for_alice is not None}")

    # Bob should have Alice's message event
    alice_msg_valid_for_bob = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND seen_by_peer_id = ?",
        (alice_msg['id'], bob_peer_id)
    )
    print(f"Alice's message valid for Bob: {alice_msg_valid_for_bob is not None}")

    # Charlie should NOT have Alice's or Bob's messages
    alice_msg_valid_for_charlie = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND seen_by_peer_id = ?",
        (alice_msg['id'], charlie_peer_id)
    )
    bob_msg_valid_for_charlie = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND seen_by_peer_id = ?",
        (bob_msg['id'], charlie_peer_id)
    )
    print(f"Alice's message valid for Charlie: {alice_msg_valid_for_charlie is not None}")
    print(f"Bob's message valid for Charlie: {bob_msg_valid_for_charlie is not None}")

    # Assertions
    assert bob_msg_valid_for_alice, "Alice should have Bob's message"
    assert alice_msg_valid_for_bob, "Bob should have Alice's message"
    assert not alice_msg_valid_for_charlie, "Charlie should NOT have Alice's message"
    assert not bob_msg_valid_for_charlie, "Charlie should NOT have Bob's message"

    print("✓ All tests passed! Three-player message transit works correctly.")

    # Re-projection test
    from tests.utils import assert_reprojection
    assert_reprojection(db)

    # Idempotency test
    from tests.utils import assert_idempotency
    assert_idempotency(db, num_trials=10, max_repetitions=5)

    # Convergence test
    from tests.utils import assert_convergence
    assert_convergence(db, num_trials=10)


if __name__ == '__main__':
    test_three_player_messaging()
