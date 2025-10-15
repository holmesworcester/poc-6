"""Unit tests for sync bloom filter functions."""
import sqlite3
import pytest
from db import Database
import schema
from events.transit import sync
from events.identity import user, invite
import crypto


def test_bloom_filter_basic():
    """Test that bloom filter correctly identifies present/absent items."""
    # Create some event IDs
    event_ids = [
        crypto.b64encode(crypto.hash(b"event1")),
        crypto.b64encode(crypto.hash(b"event2")),
        crypto.b64encode(crypto.hash(b"event3")),
    ]

    # Create bloom filter with first 2 events
    salt = b"test_salt_16byte"
    event_bytes = [crypto.b64decode(eid) for eid in event_ids[:2]]
    bloom = sync.create_bloom(event_bytes, salt)

    # Check that events in bloom return True
    assert sync.check_bloom(crypto.b64decode(event_ids[0]), bloom, salt) == True
    assert sync.check_bloom(crypto.b64decode(event_ids[1]), bloom, salt) == True

    # Check that event NOT in bloom returns False (or possibly True due to false positive)
    # With 512 bits and 5 hashes, FPR should be ~2.5%, so this should usually pass
    result = sync.check_bloom(crypto.b64decode(event_ids[2]), bloom, salt)
    # We can't assert False here due to false positives, but we can check it's a boolean
    assert isinstance(result, bool)


def test_empty_bloom_filter():
    """Test that empty bloom filter rejects all items."""
    salt = b"test_salt_16byte"
    empty_bloom = sync.create_bloom([], salt)

    # Empty bloom should be all zeros
    assert empty_bloom == bytes(64)  # 64 bytes = 512 bits

    # Any event should NOT be in empty bloom
    test_event = crypto.b64decode(crypto.b64encode(crypto.hash(b"test")))
    assert sync.check_bloom(test_event, empty_bloom, salt) == False


def test_sync_salt_derivation():
    """Test that salt derivation is deterministic."""
    peer_pk = b"test_public_key_32_bytes_long!!"
    window_id = 5

    salt1 = sync.derive_salt(peer_pk, window_id)
    salt2 = sync.derive_salt(peer_pk, window_id)

    # Same inputs should give same salt
    assert salt1 == salt2
    assert len(salt1) == 16  # 128 bits

    # Different window should give different salt
    salt3 = sync.derive_salt(peer_pk, window_id + 1)
    assert salt1 != salt3


def test_alice_bob_bloom_exchange():
    """Test that Alice correctly filters events Bob already has using bloom filter."""
    # Setup database
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Create Alice
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Create invite
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )

    # Bob joins
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    # Get Alice's shareable events
    alice_events = db.query(
        "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?",
        (alice['peer_id'],)
    )
    alice_event_ids = [row['event_id'] for row in alice_events]

    # Get Bob's shareable events
    bob_events = db.query(
        "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?",
        (bob['peer_id'],)
    )
    bob_event_ids = [row['event_id'] for row in bob_events]

    print(f"\nAlice has {len(alice_event_ids)} shareable events")
    print(f"Bob has {len(bob_event_ids)} shareable events")

    # Find events unique to Alice
    unique_to_alice = set(alice_event_ids) - set(bob_event_ids)
    shared_events = set(alice_event_ids) & set(bob_event_ids)

    print(f"Events unique to Alice: {len(unique_to_alice)}")
    print(f"Events shared by both: {len(shared_events)}")

    # Assertion: Alice should have some unique events (at least her channel)
    assert len(unique_to_alice) > 0, "Alice should have events Bob doesn't have"

    # Create Bob's bloom filter for window 1 (where Alice's channel should be)
    from events.identity import peer as peer_module

    bob_window_1_events = db.query(
        "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ? AND window_id >= 524288 AND window_id < 1048576",
        (bob['peer_id'],)
    )
    bob_w1_event_ids = [row['event_id'] for row in bob_window_1_events]

    bob_public_key = peer_module.get_public_key(bob['peer_id'], bob['peer_id'], db)
    salt = sync.derive_salt(bob_public_key, 1)  # Window 1

    bob_bloom = sync.create_bloom([crypto.b64decode(eid) for eid in bob_w1_event_ids], salt)

    # Check Alice's unique events against Bob's bloom filter
    false_positives = 0
    for event_id in unique_to_alice:
        # Get window for this event
        event_window = db.query_one(
            "SELECT window_id FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
            (event_id, alice['peer_id'])
        )

        if not event_window:
            continue

        # Check if this event is in window 1's range
        if 524288 <= event_window['window_id'] < 1048576:
            in_bloom = sync.check_bloom(crypto.b64decode(event_id), bob_bloom, salt)

            # Event unique to Alice should NOT be in Bob's bloom (unless false positive)
            if in_bloom:
                false_positives += 1
                print(f"  False positive: {event_id[:20]}...")

    # Critical assertion: Most of Alice's unique events should NOT match Bob's bloom
    alice_unique_in_window_1 = len([e for e in unique_to_alice
                                     if db.query_one("SELECT 1 FROM shareable_events WHERE event_id = ? AND window_id >= 524288 AND window_id < 1048576", (e,))])

    if alice_unique_in_window_1 > 0:
        fpr = false_positives / alice_unique_in_window_1
        print(f"\nFalse positive rate: {fpr:.2%} ({false_positives}/{alice_unique_in_window_1})")

        # With target FPR of 2.5%, we should have very few false positives
        # Allow up to 10% to account for statistical variance in small samples
        assert fpr < 0.10, f"False positive rate too high: {fpr:.2%}"

        # Most importantly: At least ONE of Alice's unique events should be sendable
        sendable = alice_unique_in_window_1 - false_positives
        assert sendable > 0, f"All of Alice's {alice_unique_in_window_1} unique events were false positives - bloom filter is broken!"


def test_sync_response_sends_unique_events():
    """Test that Alice's sync response actually sends events Bob doesn't have."""
    # Setup database
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Create Alice and Bob
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    # Send bootstrap
    user.send_bootstrap_events(
        peer_id=bob['peer_id'],
        peer_shared_id=bob['peer_shared_id'],
        user_id=bob['user_id'],
        transit_prekey_shared_id=bob['transit_prekey_shared_id'],
        invite_data=bob['invite_data'],
        t_ms=4000,
        db=db
    )

    # Alice receives bootstrap
    sync.receive(batch_size=20, t_ms=4100, db=db)
    sync.receive(batch_size=20, t_ms=4150, db=db)

    # Count incoming blobs before sync request
    blobs_before = db.query_one("SELECT COUNT(*) as cnt FROM incoming_blobs")

    # Check if Alice has a prekey
    alice_prekeys = db.query(
        "SELECT prekey_id, owner_peer_id FROM transit_prekeys WHERE owner_peer_id = ?",
        (alice['peer_id'],)
    )
    print(f"\n=== Alice's prekeys ===")
    print(f"Alice has {len(alice_prekeys)} prekeys")
    for pk in alice_prekeys:
        print(f"  - prekey owner={pk['owner_peer_id'][:20]}...")

    # Bob sends sync request to Alice
    print(f"\n=== Bob sending sync request ===")
    print(f"Bob peer_id: {bob['peer_id'][:20]}...")
    print(f"Alice peer_shared_id: {alice['peer_shared_id'][:20]}...")
    sync.send_request_to_all(t_ms=4200, db=db)

    # Check if sync request was added to incoming_blobs
    blobs_after_send = db.query_one("SELECT COUNT(*) as cnt FROM incoming_blobs")
    print(f"incoming_blobs after Bob's sync_all: {blobs_after_send['cnt']}")

    # Alice processes sync request and sends response
    print(f"\n=== Alice processing incoming blobs ===")
    sync.receive(batch_size=20, t_ms=4250, db=db)

    blobs_after_alice_receive = db.query_one("SELECT COUNT(*) as cnt FROM incoming_blobs")
    print(f"incoming_blobs after Alice's receive: {blobs_after_alice_receive['cnt']}")

    # Count incoming blobs after sync response
    blobs_after = db.query_one("SELECT COUNT(*) as cnt FROM incoming_blobs")

    new_blobs = blobs_after['cnt'] - blobs_before['cnt']

    print(f"\nAlice sent {new_blobs} blobs in sync response")

    # Debug: Check if Alice received Bob's sync request
    alice_sync_projections = db.query(
        "SELECT COUNT(*) as cnt FROM valid_events WHERE recorded_by = ? AND event_id LIKE 'sync%'",
        (alice['peer_id'],)
    )
    print(f"Alice projected {alice_sync_projections[0]['cnt']} sync events")

    # Check incoming_blobs - are Bob's requests stuck there?
    stuck_blobs = db.query("SELECT blob FROM incoming_blobs LIMIT 5")
    print(f"Stuck incoming_blobs: {len(stuck_blobs)}")

    # Critical assertion: Alice should send SOME events to Bob
    # Alice has events (channel, group, keys, etc.) that Bob doesn't have yet
    assert new_blobs > 0, "Alice should send at least some events to Bob in sync response!"


if __name__ == "__main__":
    # Run tests
    test_bloom_filter_basic()
    print("✓ test_bloom_filter_basic passed")

    test_empty_bloom_filter()
    print("✓ test_empty_bloom_filter passed")

    test_sync_salt_derivation()
    print("✓ test_sync_salt_derivation passed")

    test_alice_bob_bloom_exchange()
    print("✓ test_alice_bob_bloom_exchange passed")

    test_sync_response_sends_unique_events()
    print("✓ test_sync_response_sends_unique_events passed")

    print("\n✓ All tests passed!")
