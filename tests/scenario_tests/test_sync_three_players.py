"""
Test sync convergence with three players.

Alice creates a network and invites Bob.
Charlie creates his own separate network.

Goal: Verify that all shareable events sync correctly between Alice and Bob,
      while Charlie remains isolated.
"""
import sqlite3
from db import Database
import schema
from events.identity import user, invite, peer
import tick


def test_sync_three_players_convergence():
    """Test that shareable events sync correctly between Alice and Bob."""

    # Setup
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Alice creates a network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Alice creates an invite for Bob
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )

    # Bob joins Alice's network
    bob_peer_id, bob_peer_shared_id = peer.create(t_ms=2000, db=db)

    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    # Charlie creates his own separate network
    charlie = user.new_network(name='Charlie', t_ms=3000, db=db)

    db.commit()

    print("\n=== Initial State ===")
    alice_shareable = set(row['event_id'] for row in db.query(
        "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?",
        (alice['peer_id'],)
    ))
    bob_shareable = set(row['event_id'] for row in db.query(
        "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?",
        (bob['peer_id'],)
    ))
    charlie_shareable = set(row['event_id'] for row in db.query(
        "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?",
        (charlie['peer_id'],)
    ))

    print(f"Alice has {len(alice_shareable)} shareable events")
    print(f"Bob has {len(bob_shareable)} shareable events")
    print(f"Charlie has {len(charlie_shareable)} shareable events")

    # Track what Bob has received from Alice
    bob_has_alice_events = alice_shareable & bob_shareable
    print(f"Bob already has {len(bob_has_alice_events)}/{len(alice_shareable)} of Alice's events (from bootstrap)")

    # Run sync to convergence
    print("\n=== Running Sync ===")
    max_rounds = 100  # Increased for job-based tick system
    for round_num in range(max_rounds):
        # Run one tick cycle (send + receive)
        tick.tick(t_ms=4000 + round_num * 100, db=db)

        # Check Bob's progress
        bob_shareable_now = set(row['event_id'] for row in db.query(
            "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?",
            (bob['peer_id'],)
        ))
        bob_has_alice_now = alice_shareable & bob_shareable_now

        # Check what Bob has recorded (not just in store)
        bob_recorded_now = set()
        for row in db.query("SELECT id, blob FROM store"):
            try:
                data = json.loads(row['blob'])
                if data.get('type') == 'recorded' and data.get('recorded_by') == bob['peer_id']:
                    bob_recorded_now.add(data.get('ref_id'))
            except:
                pass
        bob_received_alice = alice_shareable & bob_recorded_now

        print(f"Round {round_num + 1}: Bob has {len(bob_has_alice_now)}/{len(alice_shareable)} "
              f"of Alice's events shareable, {len(bob_received_alice)} recorded")

        # Check if converged (Bob recorded all of Alice's shareable events)
        if bob_received_alice == alice_shareable:
            print(f"✓ Converged after {round_num + 1} rounds!")
            break
    else:
        print(f"✗ Did not converge after {max_rounds} rounds")

    # Final verification
    print("\n=== Final State ===")

    # Get final state
    alice_shareable_final = set(row['event_id'] for row in db.query(
        "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?",
        (alice['peer_id'],)
    ))
    bob_shareable_final = set(row['event_id'] for row in db.query(
        "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?",
        (bob['peer_id'],)
    ))

    # What Bob received from Alice (check if Bob recorded these events)
    import json
    bob_recorded_events = set()
    all_store = db.query("SELECT id, blob FROM store")
    for row in all_store:
        try:
            data = json.loads(row['blob'])
            if data.get('type') == 'recorded' and data.get('recorded_by') == bob['peer_id']:
                bob_recorded_events.add(data.get('ref_id'))
        except:
            pass
    bob_received_from_alice = alice_shareable_final & bob_recorded_events

    # What Alice received from Bob (check if Alice recorded these events)
    alice_recorded_events = set()
    for row in all_store:
        try:
            data = json.loads(row['blob'])
            if data.get('type') == 'recorded' and data.get('recorded_by') == alice['peer_id']:
                alice_recorded_events.add(data.get('ref_id'))
        except:
            pass
    alice_received_from_bob = bob_shareable_final & alice_recorded_events

    print(f"Alice has {len(alice_shareable_final)} shareable events")
    print(f"Bob has {len(bob_shareable_final)} shareable events")
    print(f"Bob received {len(bob_received_from_alice)}/{len(alice_shareable_final)} of Alice's events")
    print(f"Alice received {len(alice_received_from_bob)}/{len(bob_shareable_final)} of Bob's events")

    # Check blocked events
    alice_blocked = db.query_one(
        "SELECT COUNT(*) as cnt FROM blocked_events_ephemeral WHERE recorded_by = ?",
        (alice['peer_id'],)
    )['cnt']
    bob_blocked = db.query_one(
        "SELECT COUNT(*) as cnt FROM blocked_events_ephemeral WHERE recorded_by = ?",
        (bob['peer_id'],)
    )['cnt']

    print(f"\nAlice blocked: {alice_blocked}")
    print(f"Bob blocked: {bob_blocked}")

    # Find missing events
    missing_from_bob = alice_shareable_final - bob_recorded_events
    missing_from_alice = bob_shareable_final - alice_recorded_events

    if missing_from_bob:
        print(f"\n⚠️  Bob is missing {len(missing_from_bob)} of Alice's shareable events:")
        for event_id in list(missing_from_bob)[:5]:  # Show first 5
            print(f"  - {event_id[:20]}...")

    if missing_from_alice:
        print(f"\n⚠️  Alice is missing {len(missing_from_alice)} of Bob's shareable events:")
        for event_id in list(missing_from_alice)[:5]:  # Show first 5
            print(f"  - {event_id[:20]}...")

    # Charlie isolation check
    # Charlie should only "have" events he's recorded (not just what's in global store)
    charlie_has_events = set(row['event_id'] for row in db.query(
        "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?",
        (charlie['peer_id'],)
    ))
    charlie_has_alice = charlie_has_events & alice_shareable_final
    charlie_has_bob = charlie_has_events & bob_shareable_final

    print(f"\nCharlie isolation check:")
    print(f"  Charlie has {len(charlie_has_events)} shareable events (his own)")
    print(f"  Charlie has {len(charlie_has_alice)} of Alice's events (should be 0)")
    print(f"  Charlie has {len(charlie_has_bob)} of Bob's events (should be 0)")

    # Assertions
    assert len(missing_from_bob) == 0, \
        f"Bob should receive all {len(alice_shareable_final)} of Alice's shareable events, " \
        f"but is missing {len(missing_from_bob)}: {list(missing_from_bob)[:3]}"

    assert len(missing_from_alice) == 0, \
        f"Alice should receive all {len(bob_shareable_final)} of Bob's shareable events, " \
        f"but is missing {len(missing_from_alice)}: {list(missing_from_alice)[:3]}"

    assert charlie_has_alice == set(), \
        f"Charlie should not have Alice's events (isolated network), " \
        f"but has {len(charlie_has_alice)}: {list(charlie_has_alice)[:3]}"

    assert charlie_has_bob == set(), \
        f"Charlie should not have Bob's events (isolated network), " \
        f"but has {len(charlie_has_bob)}: {list(charlie_has_bob)[:3]}"

    print("\n✅ All sync assertions passed!")
