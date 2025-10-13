"""
Scenario test: Three players with message transit via sync.

Alice creates a network. Bob joins Alice's network via invite.
Charlie creates his own separate network.

Tests:
- Alice and Bob exchange sync requests and receive each other's messages
- Charlie is isolated (separate network)
- Charlie only sees their own messages
"""
import sqlite3
import pytest
from db import Database
import schema
from events.transit import sync, recorded
from events.content import message
from events.identity import user
import store
import crypto


def test_three_player_messaging():
    """Three peers: Alice creates network, Bob joins, Charlie separate."""

    # Configure logging to show INFO level for debugging sync issues
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(levelname)-8s %(name)s:%(filename)s:%(lineno)d %(message)s',
        force=True  # Override existing configuration
    )

    # Also set level on root logger
    logging.getLogger().setLevel(logging.INFO)

    # Setup: Initialize in-memory database
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Alice creates a network (implicit network via first group)
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Alice creates an invite link for Bob to join
    from events.identity import invite
    invite_id, invite_link, invite_data = invite.create(
        inviter_peer_id=alice['peer_id'],
        inviter_peer_shared_id=alice['peer_shared_id'],
        t_ms=1500,
        db=db
    )

    # Bob joins Alice's network via invite
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    # Charlie creates his own separate network
    charlie = user.new_network(name='Charlie', t_ms=3000, db=db)

    # Bootstrap: Fully realistic protocol
    # Bob sends bootstrap events + sync request (all wrapped with invite key / Alice's prekey)
    user.send_bootstrap_events(
        peer_id=bob['peer_id'],
        peer_shared_id=bob['peer_shared_id'],
        user_id=bob['user_id'],
        transit_prekey_shared_id=bob['transit_prekey_shared_id'],
        invite_data=bob['invite_data'],
        t_ms=4000,
        db=db
    )

    # Alice receives Bob's bootstrap events, learns about Bob
    sync.receive(batch_size=20, t_ms=4100, db=db)

    # Process any unblocked events (sync requests that were waiting for peer_shared)
    sync.receive(batch_size=20, t_ms=4150, db=db)

    # Alice receives Bob's sync request, sends sync response with her events
    sync.receive(batch_size=20, t_ms=4200, db=db)

    # Bob receives Alice's sync response with her peer_shared, prekey_shared, etc.
    sync.receive(batch_size=20, t_ms=4300, db=db)

    # Continue with bloom sync to exchange remaining events (group, channel, keys)
    sync.sync_all(t_ms=4400, db=db)
    sync.receive(batch_size=20, t_ms=4500, db=db)
    sync.receive(batch_size=20, t_ms=4600, db=db)

    # Additional sync rounds to ensure all events flow through
    sync.sync_all(t_ms=4700, db=db)
    sync.receive(batch_size=20, t_ms=4800, db=db)
    sync.receive(batch_size=20, t_ms=4900, db=db)

    # Extra sync rounds to ensure Bob receives Alice's group event
    for i in range(5):
        sync.sync_all(t_ms=5000 + i*100, db=db)
        sync.receive(batch_size=20, t_ms=5000 + i*100 + 50, db=db)

    db.commit()

    # Each peer creates a message
    alice_msg = message.create_message(
        params={
            'content': 'Hello from Alice',
            'channel_id': alice['channel_id'],
            'group_id': alice['group_id'],
            'peer_id': alice['peer_id'],
            'peer_shared_id': alice['peer_shared_id'],
            'key_id': alice['key_id']
        },
        t_ms=5000,
        db=db
    )

    bob_msg = message.create_message(
        params={
            'content': 'Hello from Bob',
            'channel_id': bob['channel_id'],  # Bob joined Alice's network, uses same channel
            'group_id': bob['group_id'],      # Same group
            'peer_id': bob['peer_id'],
            'peer_shared_id': bob['peer_shared_id'],
            'key_id': bob['key_id']           # Same key
        },
        t_ms=6000,
        db=db
    )

    charlie_msg = message.create_message(
        params={
            'content': 'Hello from Charlie',
            'channel_id': charlie['channel_id'],
            'group_id': charlie['group_id'],
            'peer_id': charlie['peer_id'],
            'peer_shared_id': charlie['peer_shared_id'],
            'key_id': charlie['key_id']
        },
        t_ms=7000,
        db=db
    )

    # Round 1: Send sync requests (all peers sync with peers they've seen)
    sync.sync_all(t_ms=8000, db=db)

    # Receive sync requests - this unwraps requests and auto-sends responses
    sync.receive(batch_size=10, t_ms=9000, db=db)

    # Round 2: Receive sync responses
    sync.receive(batch_size=100, t_ms=10000, db=db)

    # Round 3: Sync window 1 (with w=1, there are 2 windows: 0 and 1)
    sync.sync_all(t_ms=11000, db=db)

    # Receive sync requests for window 1
    sync.receive(batch_size=10, t_ms=12000, db=db)

    # Receive sync responses for window 1
    sync.receive(batch_size=100, t_ms=13000, db=db)

    # Round 4: Additional sync rounds for convergence
    for round_num in range(10):
        base_time = 14000 + (round_num * 1000)
        sync.sync_all(t_ms=base_time, db=db)
        sync.receive(batch_size=100, t_ms=base_time + 100, db=db)
        sync.receive(batch_size=100, t_ms=base_time + 200, db=db)

    # Debug: Check peers_shared table
    alice_peers_shared = db.query(
        "SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?",
        (alice['peer_id'],)
    )
    bob_peers_shared = db.query(
        "SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?",
        (bob['peer_id'],)
    )
    print(f"\nAlice's peers_shared table has {len(alice_peers_shared)} entries:")
    for ps in alice_peers_shared:
        is_bob = ps['peer_shared_id'] == bob['peer_shared_id']
        is_alice = ps['peer_shared_id'] == alice['peer_shared_id']
        print(f"  - {ps['peer_shared_id'][:20]}... (Bob's? {is_bob}, Alice's? {is_alice})")
    print(f"Bob's peers_shared table has {len(bob_peers_shared)} entries:")
    for ps in bob_peers_shared:
        is_bob = ps['peer_shared_id'] == bob['peer_shared_id']
        is_alice = ps['peer_shared_id'] == alice['peer_shared_id']
        print(f"  - {ps['peer_shared_id'][:20]}... (Bob's? {is_bob}, Alice's? {is_alice})")

    # Debug: Check bootstrap_status entries
    all_bootstrap_status = db.query("SELECT peer_id, recorded_by, created_network, joined_network FROM bootstrap_status")
    print(f"\nAll bootstrap_status entries ({len(all_bootstrap_status)}):")
    for entry in all_bootstrap_status:
        peer_is_alice = entry['peer_id'] == alice['peer_id']
        peer_is_bob = entry['peer_id'] == bob['peer_id']
        recorded_is_alice = entry['recorded_by'] == alice['peer_id']
        recorded_is_bob = entry['recorded_by'] == bob['peer_id']
        print(f"  peer_id={entry['peer_id'][:20]}... (Alice? {peer_is_alice}, Bob? {peer_is_bob}) recorded_by={entry['recorded_by'][:20]}... (Alice? {recorded_is_alice}, Bob? {recorded_is_bob}) created={entry['created_network']} joined={entry['joined_network']}")

    # Debug: Check sync state
    alice_syncing_with = db.query(
        "SELECT to_peer_id FROM sync_state_ephemeral WHERE from_peer_id = ?",
        (alice['peer_id'],)
    )
    bob_syncing_with = db.query(
        "SELECT to_peer_id FROM sync_state_ephemeral WHERE from_peer_id = ?",
        (bob['peer_id'],)
    )
    print(f"\nAlice (peer_id={alice['peer_id'][:20]}...) is syncing with {len(alice_syncing_with)} peers:")
    for p in alice_syncing_with:
        print(f"  - {p['to_peer_id'][:20]}... (Bob's peer_shared? {p['to_peer_id'] == bob['peer_shared_id']})")
    print(f"Bob (peer_id={bob['peer_id'][:20]}...) is syncing with {len(bob_syncing_with)} peers:")
    for p in bob_syncing_with:
        print(f"  - {p['to_peer_id'][:20]}... (Alice's peer_shared? {p['to_peer_id'] == alice['peer_shared_id']})")
    print(f"Alice's peer_shared_id: {alice['peer_shared_id'][:20]}")
    print(f"Bob's peer_shared_id: {bob['peer_shared_id'][:20]}")

    # Debug: Check if Bob's peer_shared is in Alice's valid_events
    alice_has_bob_peer_shared = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (bob['peer_shared_id'], alice['peer_id'])
    )
    print(f"Alice has Bob's peer_shared in valid_events: {alice_has_bob_peer_shared is not None}")

    # Debug: Check if Bob's message is in Bob's shareable_events
    bob_message_shareable = db.query_one(
        "SELECT window_id, created_at, recorded_at FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
        (bob_msg['id'], bob['peer_id'])
    )
    print(f"Bob's message is in Bob's shareable_events: {bob_message_shareable is not None}")
    if bob_message_shareable:
        print(f"  window_id={bob_message_shareable['window_id']}, created_at={bob_message_shareable['created_at']}, recorded_at={bob_message_shareable['recorded_at']}")
    print(f"Bob's message ID: {bob_msg['id'][:20]}")

    # Debug: Check if Bob's message is in Alice's valid_events
    alice_has_bob_message = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (bob_msg['id'], alice['peer_id'])
    )
    print(f"Alice has Bob's message in valid_events: {alice_has_bob_message is not None}")

    # Verify event visibility via valid_events table
    # Alice should have received Bob's events (peer_shared, group, channel, message)
    alice_valid_events = db.query(
        "SELECT event_id FROM valid_events WHERE recorded_by = ?",
        (alice['peer_id'],)
    )
    print(f"Alice has {len(alice_valid_events)} valid events")

    # Bob should have received Alice's events
    bob_valid_events = db.query(
        "SELECT event_id FROM valid_events WHERE recorded_by = ?",
        (bob['peer_id'],)
    )
    print(f"Bob has {len(bob_valid_events)} valid events")

    # Charlie should only have his own events
    charlie_valid_events = db.query(
        "SELECT event_id FROM valid_events WHERE recorded_by = ?",
        (charlie['peer_id'],)
    )
    print(f"Charlie has {len(charlie_valid_events)} valid events")

    # Check specific message events in store
    alice_msg_blob = store.get(alice_msg['id'], db)
    bob_msg_blob = store.get(bob_msg['id'], db)
    charlie_msg_blob = store.get(charlie_msg['id'], db)

    # Alice should have Bob's message event
    bob_msg_valid_for_alice = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (bob_msg['id'], alice['peer_id'])
    )
    print(f"Bob's message valid for Alice: {bob_msg_valid_for_alice is not None}")

    # Bob should have Alice's message event
    alice_msg_valid_for_bob = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (alice_msg['id'], bob['peer_id'])
    )
    print(f"Alice's message valid for Bob: {alice_msg_valid_for_bob is not None}")

    # Charlie should NOT have Alice's or Bob's messages
    alice_msg_valid_for_charlie = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (alice_msg['id'], charlie['peer_id'])
    )
    bob_msg_valid_for_charlie = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (bob_msg['id'], charlie['peer_id'])
    )
    print(f"Alice's message valid for Charlie: {alice_msg_valid_for_charlie is not None}")
    print(f"Bob's message valid for Charlie: {bob_msg_valid_for_charlie is not None}")

    # Assertions
    assert bob_msg_valid_for_alice, "Alice should have Bob's message"
    assert alice_msg_valid_for_bob, "Bob should have Alice's message"
    assert not alice_msg_valid_for_charlie, "Charlie should NOT have Alice's message"
    assert not bob_msg_valid_for_charlie, "Charlie should NOT have Bob's message"

    print("✓ All tests passed! Three-player message transit works correctly.")

    # Debug: Check channels table before re-projection
    all_channels = db.query("SELECT channel_id, name, recorded_by FROM channels")
    print(f"\nChannels table ({len(all_channels)} rows) before re-projection:")
    for ch in all_channels:
        is_alice_viewer = ch['recorded_by'] == alice['peer_id']
        is_bob_viewer = ch['recorded_by'] == bob['peer_id']
        is_charlie_viewer = ch['recorded_by'] == charlie['peer_id']
        print(f"  channel={ch['name']} id={ch['channel_id'][:20]}... recorded_by={ch['recorded_by'][:20]}... (Alice? {is_alice_viewer}, Bob? {is_bob_viewer}, Charlie? {is_charlie_viewer})")

    # Re-projection test
    from tests.utils import assert_reprojection
    assert_reprojection(db)

    # Idempotency test
    from tests.utils import assert_idempotency
    assert_idempotency(db, num_trials=10, max_repetitions=5)

    # Convergence test
    from tests.utils import assert_convergence
    assert_convergence(db)


if __name__ == '__main__':
    test_three_player_messaging()
