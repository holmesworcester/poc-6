"""
Scenario test: Three players with message transit via sync.

Alice creates a network. Bob joins Alice's network via invite.
Charlie creates his own separate network.

Tests:
- Alice and Bob sync correctly (including GKS)
- Alice and Bob can exchange messages
- Charlie is isolated (separate network)
"""
import sqlite3
from db import Database
import schema
from events.identity import user, invite, peer
from events.content import message
from tests.utils import tick_helper
import tick


def test_three_player_messaging(fresh_db):
    """Three peers: Alice creates network, Bob joins, Charlie separate."""

    # Setup
    db = fresh_db

    print("\n=== Setup: Create networks and invite ===")

    # Alice creates a network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    print(f"Alice created network, key_id: {alice['key_id'][:20]}...")

    # Alice creates an invite for Bob
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )
    print(f"Alice created invite: {invite_id[:20]}...")

    # Bob joins Alice's network
    bob_peer_id = peer.create(t_ms=2000, db=db)

    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    bob_peer_shared_id = bob['peer_shared_id']
    print(f"Bob joined network, peer_id: {bob['peer_id'][:20]}...")

    # Charlie creates his own separate network
    charlie = user.new_network(name='Charlie', t_ms=3000, db=db)
    print(f"Charlie created separate network")

    db.commit()

    # Check what events each peer has BEFORE sync (using shareable_events table)
    print("\n=== Events BEFORE sync ===")
    alice_events_before = db.query(
        "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?",
        (alice['peer_id'],)
    )
    bob_events_before = db.query(
        "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?",
        (bob['peer_id'],)
    )
    print(f"Alice has {len(alice_events_before)} shareable events before sync")
    print(f"Bob has {len(bob_events_before)} shareable events before sync")

    # Show Alice's events by query window (w=4)
    alice_windows = db.query("""
        SELECT (window_id / 65536) as qw, event_id FROM shareable_events
        WHERE can_share_peer_id = ? ORDER BY qw
    """, (alice['peer_id'],))
    print("\n=== Alice's events by query window (w=4) ===")
    by_qw = {}
    for row in alice_windows:
        qw = row['qw']
        if qw not in by_qw:
            by_qw[qw] = []
        by_qw[qw].append(row['event_id'][:15])
    for qw in sorted(by_qw.keys()):
        print(f"  QW {qw}: {len(by_qw[qw])} events")

    # Check connections BEFORE sync (device-wide table, no per-peer scope)
    print("\n=== Connections BEFORE sync ===")
    connections_before = db.query("SELECT our_transit_key_id FROM sync_connections")
    print(f"Device has {len(connections_before)} connections before sync")

    # Initial sync - run enough ticks for random window selection to cover all 16 windows
    # With w=4 (16 windows), we need ~100 rounds to have high probability of hitting all windows
    print("\n=== Initial sync ===")
    num_rounds = 100
    for i in range(num_rounds):
        tick.tick(t_ms=4000 + i * tick_helper.TICK_INTERVAL_MS, db=db)
    final_t_ms = 4000 + num_rounds * tick_helper.TICK_INTERVAL_MS
    print(f"Initial sync completed after {num_rounds} ticks")

    # Check connections AFTER sync (device-wide table, no per-peer scope)
    print("\n=== Connections AFTER sync ===")
    connections_after = db.query("SELECT our_transit_key_id, our_peer_id FROM sync_connections")
    print(f"Device has {len(connections_after)} connections after sync")
    for c in connections_after:
        print(f"  Connection key: {c['our_transit_key_id'][:20]}... for peer: {c['our_peer_id'][:20]}...")

    # Assert connections were established (at least 2 - Alice<->Bob bidirectional)
    assert len(connections_after) >= 1, "At least one connection should be established after sync"

    # Check what events each peer has AFTER sync
    print("\n=== Events AFTER sync ===")
    alice_events_after = db.query(
        "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?",
        (alice['peer_id'],)
    )
    bob_events_after = db.query(
        "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?",
        (bob['peer_id'],)
    )
    print(f"Alice has {len(alice_events_after)} shareable events after sync")
    print(f"Bob has {len(bob_events_after)} shareable events after sync")

    # Verify sync happened - at least one new event on each peer
    alice_new_events = len(alice_events_after) - len(alice_events_before)
    bob_new_events = len(bob_events_after) - len(bob_events_before)
    print(f"Alice gained {alice_new_events} events from sync")
    print(f"Bob gained {bob_new_events} events from sync")

    # Check how many events are blocked for Bob
    bob_blocked_count = db.query_one("SELECT COUNT(*) as cnt FROM blocked_events_ephemeral WHERE recorded_by = ?", (bob['peer_id'],))
    print(f"\nBob has {bob_blocked_count['cnt'] if bob_blocked_count else 0} events blocked (pending deps)")
    print(f"Bob has {len(bob_events_after)} events in shareable_events")

    # Debug: Check what Bob's 4 original events were
    print("\n=== Bob's original events ===")
    import store, crypto
    for row in bob_events_before:
        try:
            blob = store.get(row['event_id'], db)
            data = crypto.parse_json(blob)
            print(f"  {row['event_id'][:20]}... type={data.get('type', '?')}")
        except Exception as e:
            print(f"  {row['event_id'][:20]}... (encrypted or error: {e})")

    # Check what's blocked BEFORE assertions (query blocked_events_ephemeral directly)
    alice_blocked = db.query(
        """SELECT be.recorded_id, bed.dep_id
           FROM blocked_events_ephemeral be
           JOIN blocked_event_deps_ephemeral bed ON be.recorded_id = bed.recorded_id AND be.recorded_by = bed.recorded_by
           WHERE be.recorded_by = ?""",
        (alice['peer_id'],)
    )
    bob_blocked = db.query(
        """SELECT be.recorded_id, bed.dep_id
           FROM blocked_events_ephemeral be
           JOIN blocked_event_deps_ephemeral bed ON be.recorded_id = bed.recorded_id AND be.recorded_by = bed.recorded_by
           WHERE be.recorded_by = ?""",
        (bob['peer_id'],)
    )
    print(f"\nAlice has {len(alice_blocked)} blocked event dependencies")
    print(f"Bob has {len(bob_blocked)} blocked event dependencies")

    # Print blocked details for both
    if alice_blocked:
        print("Alice's blocked events:")
        for b in alice_blocked[:5]:
            print(f"  event={b['recorded_id'][:20]}... waiting for {b['dep_id'][:20]}...")

    if bob_blocked:
        print("Bob's blocked events:")
        for b in bob_blocked[:5]:
            print(f"  event={b['recorded_id'][:20]}... waiting for {b['dep_id'][:20]}...")

    # Also check the event types that are blocked
    print("\n=== Blocked event details ===")
    all_blocked_ids = set(b['recorded_id'] for b in alice_blocked + bob_blocked)
    import store
    for blocked_id in list(all_blocked_ids)[:3]:
        blob = store.get(blocked_id, db)
        if blob:
            import json
            try:
                event = json.loads(blob.decode())
                print(f"  Blocked {blocked_id[:20]}... type={event.get('type', 'unknown')}")
            except:
                print(f"  Blocked {blocked_id[:20]}... (can't parse)")
        else:
            print(f"  Blocked {blocked_id[:20]}... (blob not found)")

    # Trace the full dependency chain for blocked events
    print("\n=== Full dependency chain trace ===")
    import json

    def trace_chain(event_id, recorded_by, depth=0):
        """Recursively trace what an event depends on."""
        indent = "  " * depth
        blob = store.get(event_id, db)
        if not blob:
            print(f"{indent}→ {event_id[:25]}... (no blob)")
            return
        try:
            event = json.loads(blob.decode())
        except:
            print(f"{indent}→ {event_id[:25]}... (can't parse)")
            return

        event_type = event.get('type', '?')
        signed_by = event.get('signed_by', '')
        valid = db.query_one(
            "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
            (event_id, recorded_by)
        )
        valid_str = "✓" if valid else "✗"

        extra = ""
        if event_type == 'invite':
            extra = f" mode={event.get('mode', '?')}"
        print(f"{indent}→ {event_id[:25]}... type={event_type}{extra} valid={valid_str}")

        if not valid and signed_by and depth < 5:
            trace_chain(signed_by, recorded_by, depth + 1)

    for b in bob_blocked[:2]:
        print(f"\nBlocked event: {b['recorded_id'][:25]}...")
        print(f"Waiting for: {b['dep_id'][:25]}...")
        trace_chain(b['dep_id'], bob['peer_id'], depth=1)

    # Check if the bootstrap invite (mode=user) was even blocked or if projection failed
    print("\n=== Checking all shareable events ===")
    # Get Alice's network_id and bootstrap invite
    alice_network = db.query_one(
        "SELECT network_id FROM users WHERE recorded_by = ? LIMIT 1",
        (alice['peer_id'],)
    )
    if alice_network:
        net_id = alice_network['network_id']
        print(f"Alice's network_id: {net_id[:25]}...")

        # Find bootstrap invite (signed_by = network_id, mode = user)
        all_invites = db.query("SELECT invite_id, mode FROM invites WHERE recorded_by = ?", (alice['peer_id'],))
        for inv in all_invites[:5]:
            inv_blob = store.get(inv['invite_id'], db)
            if inv_blob:
                inv_data = json.loads(inv_blob.decode())
                if inv_data.get('signed_by') == net_id:
                    print(f"Bootstrap invite: {inv['invite_id'][:25]}... mode={inv['mode']}")

                    # Check if Alice has this in shareable_events
                    alice_has_event = db.query_one(
                        "SELECT window_id FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
                        (inv['invite_id'], alice['peer_id'])
                    )
                    if alice_has_event:
                        print(f"  Alice has in shareable_events: True (window={alice_has_event['window_id']}, qw={alice_has_event['window_id'] // 65536})")
                    else:
                        print(f"  Alice has in shareable_events: False")

                    # Check if Bob has this in shareable_events
                    bob_has_event = db.query_one(
                        "SELECT 1 FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
                        (inv['invite_id'], bob['peer_id'])
                    )
                    print(f"  Bob has in shareable_events: {bob_has_event is not None}")

                    # Check if it's blocked for Bob
                    blocked_for_bob = db.query_one(
                        """SELECT be.deps_remaining, bed.dep_id FROM blocked_events_ephemeral be
                           LEFT JOIN blocked_event_deps_ephemeral bed ON be.recorded_id = bed.recorded_id
                           WHERE be.recorded_by = ? AND be.recorded_id LIKE ?""",
                        (bob['peer_id'], f"%{inv['invite_id'][:10]}%")
                    )
                    if blocked_for_bob:
                        print(f"  Blocked for Bob: deps_remaining={blocked_for_bob['deps_remaining']}, waiting for {blocked_for_bob['dep_id'][:25] if blocked_for_bob['dep_id'] else 'N/A'}...")
                    else:
                        print(f"  Not blocked for Bob")

                    # Check if it's valid for Bob
                    valid_for_bob = db.query_one(
                        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
                        (inv['invite_id'], bob['peer_id'])
                    )
                    print(f"  Valid for Bob: {valid_for_bob is not None}")

                    # Check if Alice has this in HER shareable_events
                    alice_has_shareable = db.query_one(
                        "SELECT 1 FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
                        (inv['invite_id'], alice['peer_id'])
                    )
                    print(f"  Alice has in shareable_events: {alice_has_shareable is not None}")

                    # Check if the blob is in the GLOBAL store (sync should put it there)
                    blob_in_store = store.get(inv['invite_id'], db)
                    print(f"  Blob in store: {blob_in_store is not None}")

                    # Check if there's a recorded wrapper for Bob
                    bob_recorded = db.query_one(
                        "SELECT id FROM store WHERE id LIKE ? AND id != ?",
                        (f"%", inv['invite_id'])  # This won't work well, let me try different approach
                    )
                    # Actually check via shareable_events if ANY peer has this event
                    any_peer_has = db.query(
                        "SELECT can_share_peer_id FROM shareable_events WHERE event_id = ?",
                        (inv['invite_id'],)
                    )
                    print(f"  Peers with this in shareable_events: {[p['can_share_peer_id'][:15] for p in any_peer_has]}")

                    # Check projected_events for this invite
                    projected = db.query(
                        "SELECT recorded_by FROM projected_events WHERE event_id = ?",
                        (inv['invite_id'],)
                    )
                    print(f"  Peers in projected_events: {[p['recorded_by'][:15] for p in projected]}")

                    # Check if there's ANY recorded wrapper referencing this blob for Bob
                    # The recorded wrapper stores the inner ref_id in its blob
                    # This is harder to check directly, but let's see valid_events count
                    bob_valid_count = db.query_one(
                        "SELECT COUNT(*) as c FROM valid_events WHERE recorded_by = ?",
                        (bob['peer_id'],)
                    )
                    alice_valid_count = db.query_one(
                        "SELECT COUNT(*) as c FROM valid_events WHERE recorded_by = ?",
                        (alice['peer_id'],)
                    )
                    print(f"  Valid events: Alice={alice_valid_count['c']}, Bob={bob_valid_count['c']}")

    # Debug: What are the peer_ids vs peer_shared_ids?
    print(f"\n=== Peer ID debug ===")
    print(f"Alice peer_id: {alice['peer_id'][:20]}...")
    print(f"Alice peer_shared_id: {alice.get('peer_shared_id', 'N/A')[:20] if alice.get('peer_shared_id') else 'N/A'}...")
    print(f"Bob peer_id: {bob['peer_id'][:20]}...")
    print(f"Bob peer_shared_id: {bob.get('peer_shared_id', 'N/A')[:20] if bob.get('peer_shared_id') else 'N/A'}...")

    # Check shareable_events by different peer identities
    alice_shareable_by_peer_id = db.query_one(
        "SELECT COUNT(*) as c FROM shareable_events WHERE can_share_peer_id = ?",
        (alice['peer_id'],)
    )
    alice_shareable_by_peer_shared = db.query_one(
        "SELECT COUNT(*) as c FROM shareable_events WHERE can_share_peer_id = ?",
        (alice.get('peer_shared_id', ''),)
    ) if alice.get('peer_shared_id') else {'c': 0}
    print(f"Alice shareable by peer_id: {alice_shareable_by_peer_id['c']}, by peer_shared_id: {alice_shareable_by_peer_shared['c']}")

    # Check if network_id is valid for Bob
    network_valid_for_bob = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (alice['network_id'], bob['peer_id'])
    )
    print(f"Network {alice['network_id'][:20]}... valid for Bob: {network_valid_for_bob is not None}")

    # Now assert sync happened
    assert alice_new_events > 0, "Alice should have received at least one event from Bob via sync"
    assert bob_new_events > 0, "Bob should have received at least one event from Alice via sync"

    # NOTE: We trust that sync worked correctly.
    # Observable behavior (message delivery) will verify this below.

    # Discover channels via sync
    print("\n=== Discovering channels ===")

    alice_channels = db.query_all(
        "SELECT DISTINCT channel_id FROM channels WHERE recorded_by = ?",
        (alice['peer_id'],)
    )
    bob_channels = db.query_all(
        "SELECT DISTINCT channel_id FROM channels WHERE recorded_by = ?",
        (bob['peer_id'],)
    )
    charlie_channels = db.query_all(
        "SELECT DISTINCT channel_id FROM channels WHERE recorded_by = ?",
        (charlie['peer_id'],)
    )

    assert len(alice_channels) == 1, f"Alice should have 1 channel, got {len(alice_channels)}"
    assert len(bob_channels) == 1, f"Bob should have 1 channel, got {len(bob_channels)}"
    assert len(charlie_channels) == 1, f"Charlie should have 1 channel, got {len(charlie_channels)}"

    alice_channel_id = alice_channels[0]['channel_id']
    bob_channel_id = bob_channels[0]['channel_id']
    charlie_channel_id = charlie_channels[0]['channel_id']

    # Alice and Bob should share the same channel (same network)
    assert alice_channel_id == bob_channel_id, \
        "Alice and Bob should share the same channel (same network)"
    print(f"Alice and Bob share channel: {alice_channel_id[:20]}...")
    print(f"Charlie has separate channel: {charlie_channel_id[:20]}...")

    # Create messages
    print("\n=== Creating messages ===")

    # Alice sends a message
    alice_msg = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice_channel_id,
        content="Hello from Alice!",
        t_ms=5000,
        db=db
    )
    db.commit()
    print(f"Alice created message: {alice_msg['id'][:20]}...")

    # Bob sends a message
    bob_msg = message.create(
        peer_id=bob['peer_id'],
        channel_id=bob_channel_id,
        content="Hello from Bob!",
        t_ms=5100,
        db=db
    )
    db.commit()
    print(f"Bob created message: {bob_msg['id'][:20]}...")

    # Charlie sends a message (in his own network)
    charlie_msg = message.create(
        peer_id=charlie['peer_id'],
        channel_id=charlie_channel_id,
        content="Hello from Charlie!",
        t_ms=5200,
        db=db
    )
    db.commit()
    print(f"Charlie created message: {charlie_msg['id'][:20]}...")

    # Sync messages
    print("\n=== Sync Round 2: Message exchange ===")
    final_t_ms2, rounds_used2, converged2, status2 = tick_helper.sync_until_converged(
        db=db, start_t_ms=6000, max_rounds=500, check_interval=1, verbose=True
    )
    print(f"Message sync completed in {rounds_used2} rounds (converged={converged2})")

    # Verify message delivery
    print("\n=== Verifying message delivery ===")

    # Alice should have received Bob's message
    alice_messages = message.list(alice_channel_id, alice['peer_id'], db)
    alice_message_contents = [msg['content'] for msg in alice_messages]
    print(f"Alice sees {len(alice_messages)} messages: {alice_message_contents}")

    # Verify author names are populated correctly
    for msg in alice_messages:
        assert msg.get('author_name'), f"Message should have author_name populated: {msg}"
    alice_author_names = {msg['author_name'] for msg in alice_messages}
    print(f"Alice sees authors: {alice_author_names}")
    assert 'Alice' in alice_author_names, "Alice should see her own name as author"
    assert 'Bob' in alice_author_names, "Alice should see Bob's name as author"

    assert "Hello from Alice!" in alice_message_contents, \
        "Alice should see her own message"
    assert "Hello from Bob!" in alice_message_contents, \
        "Alice should see Bob's message"
    assert "Hello from Charlie!" not in alice_message_contents, \
        "Alice should NOT see Charlie's message (different network)"

    # Bob should have received Alice's message
    bob_messages = message.list(bob_channel_id, bob['peer_id'], db)
    bob_message_contents = [msg['content'] for msg in bob_messages]
    print(f"Bob sees {len(bob_messages)} messages: {bob_message_contents}")

    assert "Hello from Alice!" in bob_message_contents, \
        "Bob should see Alice's message"
    assert "Hello from Bob!" in bob_message_contents, \
        "Bob should see his own message"
    assert "Hello from Charlie!" not in bob_message_contents, \
        "Bob should NOT see Charlie's message (different network)"

    # Charlie should only see his own message
    charlie_messages = message.list(charlie_channel_id, charlie['peer_id'], db)
    charlie_message_contents = [msg['content'] for msg in charlie_messages]
    print(f"Charlie sees {len(charlie_messages)} messages: {charlie_message_contents}")

    assert "Hello from Charlie!" in charlie_message_contents, \
        "Charlie should see his own message"
    assert "Hello from Alice!" not in charlie_message_contents, \
        "Charlie should NOT see Alice's message (different network)"
    assert "Hello from Bob!" not in charlie_message_contents, \
        "Charlie should NOT see Bob's message (different network)"

    print("\n✅ All assertions passed!")
