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
from events.transit import sync, recorded, sync_debug
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
        peer_id=alice['peer_id'],
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

    print(f"\n=== STEP 1: Alice receives Bob's bootstrap events ===")
    sync.receive(batch_size=20, t_ms=4100, db=db)
    bob_has_channel = db.query_one("SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?", (bob['channel_id'], bob['peer_id']))
    print(f"Bob has channel valid: {bool(bob_has_channel)}")

    print(f"\n=== STEP 2: Process any unblocked events ===")
    sync.receive(batch_size=20, t_ms=4150, db=db)
    bob_has_channel = db.query_one("SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?", (bob['channel_id'], bob['peer_id']))
    print(f"Bob has channel valid: {bool(bob_has_channel)}")

    print(f"\n=== STEP 3: Alice processes sync request, sends response ===")
    sync.receive(batch_size=20, t_ms=4200, db=db)
    bob_has_channel = db.query_one("SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?", (bob['channel_id'], bob['peer_id']))
    print(f"Bob has channel valid: {bool(bob_has_channel)}")

    print(f"\n=== STEP 4: Bob receives Alice's sync response ===")
    sync.receive(batch_size=20, t_ms=4300, db=db)
    bob_has_channel = db.query_one("SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?", (bob['channel_id'], bob['peer_id']))
    bob_blocked = db.query_one("SELECT COUNT(*) as cnt FROM blocked_events_ephemeral WHERE recorded_by = ?", (bob['peer_id'],))
    print(f"Bob has channel valid: {bool(bob_has_channel)}")
    print(f"Bob blocked events: {bob_blocked['cnt']}")

    print(f"\n=== STEP 5: Run debug sync to convergence ===")
    send_rounds = sync_debug.send_request_to_all_debug(t_ms=4400, db=db, max_rounds=20)
    print(f"Debug sync send completed in {send_rounds} rounds")

    receive_rounds = sync_debug.receive_debug(batch_size=20, t_ms=5000, db=db, max_rounds=20)
    print(f"Debug sync receive completed in {receive_rounds} rounds")

    bob_has_channel = db.query_one("SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?", (bob['channel_id'], bob['peer_id']))
    bob_blocked = db.query_one("SELECT COUNT(*) as cnt FROM blocked_events_ephemeral WHERE recorded_by = ?", (bob['peer_id'],))
    print(f"After debug sync: Bob has channel valid: {bool(bob_has_channel)}, blocked: {bob_blocked['cnt']}")

    print(f"\n=== Verify Bob received and projected group_key_shared ===")

    # Check if Bob has Alice's network key (from GKS) in his group_keys table
    alice_network_key_id = alice['key_id']  # Alice's network key
    bob_has_network_key = db.query_one(
        "SELECT 1 FROM group_keys WHERE key_id = ? AND recorded_by = ?",
        (alice_network_key_id, bob['peer_id'])
    )
    print(f"Bob has Alice's network key: {bool(bob_has_network_key)}")

    # Find all shareable events from Alice
    alice_shareable_events = db.query(
        """SELECT se.event_id
           FROM shareable_events se
           WHERE se.can_share_peer_id = ?
           ORDER BY se.created_at""",
        (alice['peer_id'],)
    )
    print(f"Alice has {len(alice_shareable_events)} shareable events total")
    for row in alice_shareable_events:
        # Try to determine event type
        event_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (row['event_id'],))
        if event_blob:
            try:
                data = crypto.parse_json(event_blob['blob'])
                event_type = data.get('type', 'unknown')
            except:
                event_type = 'encrypted'
            # Get window_id from shareable_events
            window_row = db.query_one(
                "SELECT window_id FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
                (row['event_id'], alice['peer_id'])
            )
            window_id = window_row['window_id'] if window_row else 'N/A'
            print(f"  - {row['event_id'][:20]}... type={event_type} window={window_id}")

    # Count Bob's valid events
    bob_valid_count = db.query_one(
        "SELECT COUNT(*) as count FROM valid_events WHERE recorded_by = ?",
        (bob['peer_id'],)
    )
    print(f"Bob has {bob_valid_count['count']} valid events total")

    # Check what events Bob has received from Alice (check valid_events for events that Alice created)
    bob_has_alice_events = db.query(
        """SELECT ve.event_id
           FROM valid_events ve
           WHERE ve.recorded_by = ?
           AND ve.event_id IN (SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?)
           LIMIT 10""",
        (bob['peer_id'], alice['peer_id'])
    )
    print(f"Bob has received {len(bob_has_alice_events)} of Alice's {len(alice_shareable_events)} shareable events:")
    for row in bob_has_alice_events:
        print(f"  - {row['event_id'][:20]}...")

    # Check if Bob has received any group_key_shared events
    bob_gks_in_store = db.query(
        """SELECT s.id, v.event_id as valid_id
           FROM store s
           LEFT JOIN valid_events v ON v.event_id = s.id AND v.recorded_by = ?
           WHERE s.blob LIKE '%group_key_shared%'""",
        (bob['peer_id'],)
    )
    print(f"Bob has {len(bob_gks_in_store)} group_key_shared events in store")
    for row in bob_gks_in_store:
        print(f"  GKS event {row['id'][:20]}..., valid={bool(row['valid_id'])}")

    # Critical assertion - this will show us if sync is working
    assert bob_has_network_key, f"Bob should have Alice's network key {alice_network_key_id} after sync (received via group_key_shared)"

    # Check if channel blob is in store
    channel_in_store = db.query_one("SELECT 1 FROM store WHERE id = ?", (bob['channel_id'],))
    print(f"Channel in store: {bool(channel_in_store)}")

    # Check if channel can be unwrapped
    if channel_in_store:
        import crypto
        channel_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (bob['channel_id'],))['blob']
        plaintext, missing = crypto.unwrap(channel_blob, bob['peer_id'], db)
        print(f"Channel unwrap: plaintext={bool(plaintext)}, missing_keys={missing}")

    # Check WHICH events are blocked
    if bob_blocked['cnt'] > 0:
        print(f"\n=== Blocked Events Debug ===")
        blocked_rows = db.query("SELECT recorded_id FROM blocked_events_ephemeral WHERE recorded_by = ?", (bob['peer_id'],))
        missing_deps_set = set()
        for row in blocked_rows:
            # Get the missing deps
            deps = db.query("SELECT dep_id FROM blocked_event_deps_ephemeral WHERE recorded_id = ?", (row['recorded_id'],))
            dep_list = [d['dep_id'] for d in deps]
            missing_deps_set.update(dep_list)
            print(f"Blocked event: {row['recorded_id'][:20]}..., missing: {[d[:20] + '...' for d in dep_list]}")

        # Check if the missing deps are in store
        print(f"\n=== Missing Dependencies Analysis ===")
        for dep_id in missing_deps_set:
            in_store = db.query_one("SELECT 1 FROM store WHERE id = ?", (dep_id,))
            in_valid = db.query_one("SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?", (dep_id, bob['peer_id']))
            in_shareable = db.query_one("SELECT 1 FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?", (dep_id, alice['peer_id']))

            # Try to get event type
            event_type = "unknown"
            if in_store:
                try:
                    blob = db.query_one("SELECT blob FROM store WHERE id = ?", (dep_id,))['blob']
                    data = crypto.parse_json(blob)
                    event_type = data.get('type', 'unknown')
                except:
                    event_type = "encrypted/unparseable"

            print(f"Dep {dep_id[:20]}... type={event_type}, in_store={bool(in_store)}, in_valid={bool(in_valid)}, in_alice_shareable={bool(in_shareable)}")

            # This is the BUG: event is valid but not in store - must be ephemeral!
            if in_valid and not in_store:
                print(f"  ❌ BUG: Event is in valid_events but NOT in store! This must be an ephemeral event.")
                print(f"  Ephemeral events (sync, transit_key) should NOT be dependencies for other events!")

    db.commit()

    # Ensure Bob has the channel event marked as valid before creating message
    # This ensures it will re-project correctly
    for attempt in range(20):
        bob_has_channel_event = db.query_one(
            "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
            (bob['channel_id'], bob['peer_id'])
        )
        if bob_has_channel_event:
            break
        t_base = 5000 + (attempt * 10)
        sync.send_request_to_all(t_ms=t_base, db=db)
        sync.receive(batch_size=20, t_ms=t_base + 5, db=db)

    assert bob_has_channel_event, f"Bob should have channel event {bob['channel_id']} marked as valid after sync"

    # Verify the channel is also projected (for message.create() to work)
    bob_channel = db.query_one(
        "SELECT channel_id FROM channels WHERE channel_id = ? AND recorded_by = ?",
        (bob['channel_id'], bob['peer_id'])
    )
    assert bob_channel, f"Bob should have channel {bob['channel_id']} projected"

    # Debug: Check how many times channel is in channels table for each peer
    print(f"\nDEBUG: Channel {bob['channel_id'][:20]}... in channels table:")
    for peer_name, peer_data in [('Alice', alice), ('Bob', bob), ('Charlie', charlie)]:
        channel_rows = db.query(
            "SELECT recorded_at FROM channels WHERE channel_id = ? AND recorded_by = ?",
            (bob['channel_id'], peer_data['peer_id'])
        )
        print(f"  {peer_name}: {len(channel_rows)} rows, recorded_at={[r['recorded_at'] for r in channel_rows]}")

    # Debug: Count recorded events for the channel by peer
    print(f"\nDEBUG: Recorded events for channel {bob['channel_id'][:20]}...:")
    import crypto as crypto_mod
    channel_id_bytes = crypto_mod.b64decode(bob['channel_id'])
    all_recorded = db.query("SELECT id, blob FROM store ORDER BY rowid")
    for peer_name, peer_data in [('Alice', alice), ('Bob', bob), ('Charlie', charlie)]:
        count = 0
        for row in all_recorded:
            try:
                event_data = crypto_mod.parse_json(row['blob'])
                if event_data.get('type') == 'recorded':
                    if event_data.get('ref_id') == bob['channel_id'] and event_data.get('recorded_by') == peer_data['peer_id']:
                        count += 1
            except:
                pass
        print(f"  {peer_name}: {count} recorded events")

    # Each peer creates a message
    alice_msg = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Hello from Alice',
        t_ms=6000,
        db=db
    )

    bob_msg = message.create(
        peer_id=bob['peer_id'],
        channel_id=bob['channel_id'],
        content='Hello from Bob',
        t_ms=7000,
        db=db
    )

    charlie_msg = message.create(
        peer_id=charlie['peer_id'],
        channel_id=charlie['channel_id'],
        content='Hello from Charlie',
        t_ms=8000,
        db=db
    )

    # Round 1: Send sync requests (all peers sync with peers they've seen)
    sync.send_request_to_all(t_ms=9000, db=db)

    # Receive sync requests - this unwraps requests and auto-sends responses
    sync.receive(batch_size=10, t_ms=10000, db=db)

    # Round 2: Receive sync responses
    sync.receive(batch_size=100, t_ms=11000, db=db)

    # Round 3: Sync window 1 (with w=1, there are 2 windows: 0 and 1)
    sync.send_request_to_all(t_ms=12000, db=db)

    # Receive sync requests for window 1
    sync.receive(batch_size=10, t_ms=13000, db=db)

    # Receive sync responses for window 1
    sync.receive(batch_size=100, t_ms=14000, db=db)

    # Round 4: Additional sync rounds for convergence
    for round_num in range(10):
        base_time = 15000 + (round_num * 1000)
        sync.send_request_to_all(t_ms=base_time, db=db)
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
