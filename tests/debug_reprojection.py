#!/usr/bin/env python3
"""Debug script to understand three-player reprojection failure."""

import sqlite3
import logging
from db import Database
import schema
from events.transit import sync
from events.content import message
from events.identity import user, invite
import crypto

# Configure detailed logging
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)-8s [%(name)s] %(message)s'
)

def main():
    print("=" * 80)
    print("THREE-PLAYER REPROJECTION DEBUG")
    print("=" * 80)

    # Setup database
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Create network and users
    print("\n1. Creating Alice's network...")
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    print(f"   Alice peer_id: {alice['peer_id'][:20]}...")
    print(f"   Alice channel_id: {alice['channel_id'][:20]}...")

    print("\n2. Creating invite for Bob...")
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )

    print("\n3. Bob joins via invite...")
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    print(f"   Bob peer_id: {bob['peer_id'][:20]}...")

    print("\n4. Charlie creates separate network...")
    charlie = user.new_network(name='Charlie', t_ms=3000, db=db)
    print(f"   Charlie peer_id: {charlie['peer_id'][:20]}...")

    # Bootstrap
    print("\n5. Running bootstrap protocol...")
    user.send_bootstrap_events(
        peer_id=bob['peer_id'],
        peer_shared_id=bob['peer_shared_id'],
        user_id=bob['user_id'],
        transit_prekey_shared_id=bob['transit_prekey_shared_id'],
        invite_data=bob['invite_data'],
        t_ms=4000,
        db=db
    )

    sync.receive(batch_size=20, t_ms=4100, db=db)
    sync.receive(batch_size=20, t_ms=4150, db=db)
    sync.receive(batch_size=20, t_ms=4200, db=db)
    sync.receive(batch_size=20, t_ms=4300, db=db)

    sync.sync_all(t_ms=4400, db=db)
    sync.receive(batch_size=20, t_ms=4500, db=db)
    sync.receive(batch_size=20, t_ms=4600, db=db)

    sync.sync_all(t_ms=4700, db=db)

    print("\n6. BEFORE t=4800 receive - checking Bob's channel view...")

    # Check Alice's view for comparison
    alice_channel_alice_view = db.query(
        "SELECT * FROM channels WHERE channel_id = ? AND recorded_by = ?",
        (alice['channel_id'], alice['peer_id'])
    )
    print(f"   Alice has {len(alice_channel_alice_view)} view(s) of her own channel")
    bob_channel_before = db.query(
        "SELECT * FROM channels WHERE channel_id = ? AND recorded_by = ?",
        (alice['channel_id'], bob['peer_id'])
    )
    print(f"   Bob has {len(bob_channel_before)} view(s) of Alice's channel")

    print("\n7. Running t=4800 receive (THIS IS WHERE BOB'S VIEW APPEARS)...")
    sync.receive(batch_size=20, t_ms=4800, db=db)
    sync.receive(batch_size=20, t_ms=4900, db=db)

    # Additional sync rounds (from original test)
    for round_num in range(10):
        base_time = 15000 + (round_num * 1000)

        # Check before this round
        bob_view_before = db.query(
            "SELECT * FROM channels WHERE channel_id = ? AND recorded_by = ?",
            (alice['channel_id'], bob['peer_id'])
        )

        sync.sync_all(t_ms=base_time, db=db)
        sync.receive(batch_size=100, t_ms=base_time + 100, db=db)
        sync.receive(batch_size=100, t_ms=base_time + 200, db=db)

        # Check after this round
        bob_view_after = db.query(
            "SELECT * FROM channels WHERE channel_id = ? AND recorded_by = ?",
            (alice['channel_id'], bob['peer_id'])
        )

        if len(bob_view_after) > len(bob_view_before):
            print(f"\n*** Bob's channel view appeared during round {round_num} at t={base_time}! ***")
            if bob_view_after:
                print(f"    recorded_at={bob_view_after[0]['recorded_at']}")

    print("\n8. AFTER all sync rounds - checking Bob's channel view...")
    bob_channel_after = db.query(
        "SELECT * FROM channels WHERE channel_id = ? AND recorded_by = ?",
        (alice['channel_id'], bob['peer_id'])
    )
    print(f"   Bob has {len(bob_channel_after)} view(s) of Alice's channel")
    if bob_channel_after:
        for row in bob_channel_after:
            print(f"   Row: {row}")

    # Now test reprojection!
    print("\n=== TESTING REPROJECTION ===")
    from tests.utils.convergence import _get_projectable_event_ids, _dump_projection_state, _recreate_projection_tables, _replay_events, _states_equal

    # Capture baseline
    baseline_state = _dump_projection_state(db)
    print(f"Baseline: {len(baseline_state.get('channels', []))} channels")

    # Get events
    event_ids = _get_projectable_event_ids(db)
    print(f"Replaying {len(event_ids)} events...")

    # Wipe and replay
    _recreate_projection_tables(db)
    _replay_events(event_ids, db)

    # Compare
    current_state = _dump_projection_state(db)
    print(f"After reprojection: {len(current_state.get('channels', []))} channels")

    bob_channel_reprojected = db.query(
        "SELECT * FROM channels WHERE channel_id = ? AND recorded_by = ?",
        (alice['channel_id'], bob['peer_id'])
    )
    print(f"Bob's channel view after reprojection: {len(bob_channel_reprojected)} rows")

    equal, diff_msg = _states_equal(baseline_state, current_state)
    if equal:
        print("\n✅ REPROJECTION PASSED!")
    else:
        print(f"\n❌ REPROJECTION FAILED: {diff_msg}")

    # Check what events became valid for Bob during this receive
    print("\n9. Checking what events are valid for Bob...")
    bob_valid = db.query(
        "SELECT event_id FROM valid_events WHERE recorded_by = ?",
        (bob['peer_id'],)
    )
    print(f"   Bob has {len(bob_valid)} valid events total")

    # Check specifically for Alice's channel event
    alice_channel_valid_for_bob = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (alice['channel_id'], bob['peer_id'])
    )
    print(f"   Alice's channel event valid for Bob: {alice_channel_valid_for_bob is not None}")

    # Check blocked events
    blocked = db.query(
        "SELECT * FROM blocked_events_ephemeral WHERE recorded_by = ?",
        (bob['peer_id'],)
    )
    print(f"\n10. Bob has {len(blocked)} blocked events")
    if blocked:
        print("   First 5 blocked events:")
        for b in blocked[:5]:
            print(f"   - recorded_id={b['recorded_id'][:20]}... deps_remaining={b['deps_remaining']}")
            print(f"     missing_deps: {b['missing_deps']}")
            # Get the actual event type
            blob = db.query_one("SELECT blob FROM store WHERE id = ?", (crypto.b64decode(b['recorded_id']),))
            if blob:
                try:
                    recorded_data = crypto.parse_json(blob['blob'])
                    ref_id = recorded_data.get('ref_id')
                    ref_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (crypto.b64decode(ref_id),))
                    if ref_blob:
                        ref_data = crypto.parse_json(ref_blob['blob'])
                        print(f"     Type: {ref_data.get('type')}")
                except:
                    pass

    # Now check which event became valid that allowed the channel to project
    print("\n11. Finding what unblocked Bob's channel view...")

    # Look at the blocked event for Bob's channel view
    print(f"   Looking for blocked events with Alice's channel in deps...")
    all_blocked_deps = db.query(
        "SELECT recorded_id, dep_id FROM blocked_event_deps_ephemeral WHERE recorded_by = ?",
        (bob['peer_id'],)
    )
    print(f"   Bob has {len(all_blocked_deps)} blocked event deps total")

    if len(all_blocked_deps) == 0:
        print("   No blocked deps - channel should project!")

    # Find which deps are not valid yet
    for dep_row in all_blocked_deps:
        dep_id = dep_row['dep_id']
        recorded_id = dep_row['recorded_id']
        is_valid = db.query_one(
            "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
            (dep_id, bob['peer_id'])
        )
        if not is_valid:
            print(f"   Missing dep: {dep_id[:20]}... for recorded_id={recorded_id[:20]}...")

            # Check what event this recorded_id refers to
            recorded_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (crypto.b64decode(recorded_id),))
            if recorded_blob:
                try:
                    recorded_data = crypto.parse_json(recorded_blob['blob'])
                    ref_id = recorded_data.get('ref_id')
                    print(f"     Blocked event refs: {ref_id[:20]}...")

                    # Try to get the ref event to see its type
                    ref_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (crypto.b64decode(ref_id),))
                    if ref_blob:
                        # Try parsing as plaintext first
                        import crypto as crypto_mod
                        try:
                            ref_event = crypto_mod.parse_json(ref_blob['blob'])
                            print(f"     Blocked event is type: {ref_event.get('type')}")
                            if ref_event.get('peer_id'):
                                is_alice_peer = ref_event.get('peer_id') == alice['peer_id']
                                is_bob_peer = ref_event.get('peer_id') == bob['peer_id']
                                print(f"     Has peer_id field: {ref_event.get('peer_id')[:20]}... (Alice? {is_alice_peer}, Bob? {is_bob_peer})")
                            if ref_event.get('created_by'):
                                print(f"     Has created_by field: {ref_event.get('created_by')[:20]}...")
                        except:
                            # Maybe encrypted
                            print(f"     (event is encrypted)")
                except Exception as e:
                    print(f"     (error: {e})")

            # Try to get the dep event type
            dep_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (crypto.b64decode(dep_id),))
            if dep_blob:
                try:
                    dep_data = crypto.parse_json(dep_blob['blob'])
                    event_type = dep_data.get('type')
                    print(f"     Missing dep type: {event_type}")
                    if event_type == 'peer_shared':
                        owner_peer_id = dep_data.get('peer_id')
                        is_alice = owner_peer_id == alice['peer_id']
                        is_bob = owner_peer_id == bob['peer_id']
                        print(f"     Owner peer_id={owner_peer_id[:20]}... (Alice? {is_alice}, Bob? {is_bob})")
                    elif event_type == 'peer':
                        # This is a local peer event - check if it's Alice's or Bob's
                        is_alice_peer = dep_id == alice['peer_id']
                        is_bob_peer = dep_id == bob['peer_id']
                        print(f"     peer_id={dep_id[:20]}... (Alice? {is_alice_peer}, Bob? {is_bob_peer})")
                        print(f"     *** THIS IS A LOCAL-ONLY EVENT! Bob should never depend on it! ***")
                    elif event_type == 'group':
                        print(f"     Group name: {dep_data.get('name')}")
                        is_alice_group = dep_id == alice['group_id']
                        print(f"     Is Alice's group? {is_alice_group}")
                except Exception as e:
                    print(f"     (encrypted or parse error: {e})")

    print("\n" + "=" * 80)
    print("DEBUG COMPLETE")
    print("=" * 80)

if __name__ == '__main__':
    main()
