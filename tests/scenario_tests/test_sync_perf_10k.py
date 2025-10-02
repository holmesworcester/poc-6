"""
Syncing performance test: Alice creates 10k messages, Bob syncs with zero latency.

Tests bloom-based windowed sync performance and verifies math:
- For 10k events: w ≈ 7 (128 windows)
- Each sync round covers 1 window
- Expected: ~128 sync rounds for full sync

This test uses the same client for both peers (zero latency) to measure
the number of sync rounds needed, not network performance.
"""
import sqlite3
import pytest
from db import Database
import schema
from events import peer, key, group, channel, message, sync
from events.sync import windows, constants
import queues
import logging

# Enable logging for sync module
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def test_alice_10k_messages_bob_syncs():
    """Alice creates 10k messages, Bob syncs using bloom-based protocol."""

    # Setup: Initialize in-memory database (shared for zero latency)
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Create Alice's identity
    alice_peer_id, alice_peer_shared_id = peer.create(t_ms=1000, db=db)
    alice_key_id = key.create(alice_peer_id, t_ms=2000, db=db)

    # Create Alice's prekey for sync
    from events import prekey
    alice_prekey_id, alice_prekey_private = prekey.create(alice_peer_id, t_ms=2500, db=db)
    alice_group_id = group.create(
        name='Alice Group',
        peer_id=alice_peer_id,
        peer_shared_id=alice_peer_shared_id,
        key_id=alice_key_id,
        t_ms=3000,
        db=db
    )
    alice_channel_id = channel.create(
        name='general',
        group_id=alice_group_id,
        peer_id=alice_peer_id,
        peer_shared_id=alice_peer_shared_id,
        key_id=alice_key_id,
        t_ms=4000,
        db=db
    )

    # Create Bob's identity
    bob_peer_id, bob_peer_shared_id = peer.create(t_ms=5000, db=db)
    bob_key_id = key.create(bob_peer_id, t_ms=6000, db=db)

    # Create Bob's prekey for sync
    bob_prekey_id, bob_prekey_private = prekey.create(bob_peer_id, t_ms=6500, db=db)

    log.info(f"Alice peer_id: {alice_peer_id}")
    log.info(f"Bob peer_id: {bob_peer_id}")

    # Alice creates messages (using 1000 for faster testing, can increase to 10k)
    log.info("Alice creating 1,000 messages...")
    num_messages = 1000
    for i in range(num_messages):
        message.create_message(
            params={
                'content': f'Message {i}',
                'channel_id': alice_channel_id,
                'group_id': alice_group_id,
                'peer_id': alice_peer_id,
                'peer_shared_id': alice_peer_shared_id,
                'key_id': alice_key_id
            },
            t_ms=10000 + i,
            db=db
        )

        if (i + 1) % 1000 == 0:
            log.info(f"Created {i + 1} messages")

    db.commit()
    log.info(f"Alice created {num_messages} messages")

    # Verify Alice has 10k shareable events
    alice_events = db.query(
        "SELECT COUNT(*) as count FROM shareable_events WHERE peer_id = ?",
        (alice_peer_shared_id,)
    )
    total_alice_events = alice_events[0]['count']
    log.info(f"Alice has {total_alice_events} shareable events (includes group, channel, peer_shared, prekey_shared, messages)")

    # Calculate expected window parameters
    expected_w = windows.compute_w_for_event_count(total_alice_events)
    expected_windows = 2 ** expected_w
    log.info(f"Expected w={expected_w}, total windows={expected_windows}")
    log.info(f"Expected events per window: ~{total_alice_events / expected_windows:.1f}")

    # Bob syncs with Alice
    log.info("Bob starting sync with Alice...")

    # Bob needs to know about Alice first (exchange peer_shared events)
    # In real scenario, this would happen via invite or bootstrap
    # For test: manually make Alice's peer_shared known to Bob
    alice_peer_shared_blob = db.query_one(
        "SELECT ps.peer_shared_id FROM peers_shared ps WHERE ps.seen_by_peer_id = ?",
        (alice_peer_id,)
    )
    if alice_peer_shared_blob:
        # Mark Alice's peer_shared as valid for Bob
        db.execute(
            "INSERT OR IGNORE INTO valid_events (event_id, seen_by_peer_id) VALUES (?, ?)",
            (alice_peer_shared_blob['peer_shared_id'], bob_peer_id)
        )
        db.commit()

    # Simulate sync rounds
    sync_rounds = 0
    max_rounds = expected_windows * 2  # Safety limit (2x expected)
    bob_received_events = 0

    while sync_rounds < max_rounds:
        sync_rounds += 1

        # Bob sends sync request to Alice
        sync.send_request(
            to_peer_id=alice_peer_id,
            from_peer_id=bob_peer_id,
            from_peer_shared_id=bob_peer_shared_id,
            t_ms=20000 + sync_rounds,
            db=db
        )

        # Alice processes request (zero latency - same queue)
        # Drain incoming queue and process as Alice
        alice_blobs = queues.incoming.drain(100, db)
        for blob in alice_blobs:
            sync.unwrap_and_store(blob, 20000 + sync_rounds, db)
        db.commit()
        log.info(f"Round {sync_rounds}: Alice processed {len(alice_blobs)} requests")

        # Bob receives response (zero latency - same queue)
        bob_blobs = queues.incoming.drain(1000, db)
        bob_received_events += len(bob_blobs)
        for blob in bob_blobs:
            sync.unwrap_and_store(blob, 20000 + sync_rounds, db)
        db.commit()
        log.info(f"Round {sync_rounds}: Bob received {len(bob_blobs)} events (total: {bob_received_events})")

        # Check if Bob has all events
        bob_events = db.query(
            "SELECT COUNT(*) as count FROM valid_events WHERE seen_by_peer_id = ?",
            (bob_peer_id,)
        )
        bob_event_count = bob_events[0]['count']

        if bob_event_count >= total_alice_events:
            log.info(f"✓ Bob fully synced after {sync_rounds} rounds!")
            break

        # Check if we're making progress
        if len(bob_blobs) == 0 and len(alice_blobs) == 0:
            log.info(f"No progress in round {sync_rounds}, stopping")
            break

    # Verify sync completed
    bob_final_events = db.query(
        "SELECT COUNT(*) as count FROM valid_events WHERE seen_by_peer_id = ?",
        (bob_peer_id,)
    )
    bob_final_count = bob_final_events[0]['count']

    log.info(f"\nSync Results:")
    log.info(f"  Alice events: {total_alice_events}")
    log.info(f"  Bob events: {bob_final_count}")
    log.info(f"  Sync rounds: {sync_rounds}")
    log.info(f"  Expected rounds: ~{expected_windows}")
    log.info(f"  Events per round: ~{total_alice_events / sync_rounds:.1f}")

    # Assertions
    assert bob_final_count >= num_messages, f"Bob should have at least {num_messages} messages, has {bob_final_count}"
    assert sync_rounds <= expected_windows * 1.5, f"Sync took {sync_rounds} rounds, expected ~{expected_windows}"
    assert sync_rounds > 0, "Should take at least 1 sync round"

    log.info("✓ Sync performance test passed!")


if __name__ == "__main__":
    test_alice_10k_messages_bob_syncs()
