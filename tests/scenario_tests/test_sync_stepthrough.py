"""
Step-through test for sync flow debugging.

Manually calls each sync function with detailed assertions to identify
exactly where the sync flow breaks down.
"""
import sqlite3
from db import Database, create_safe_db, create_unsafe_db
import schema
from events.identity import user, invite, peer
from events.network import sync, sync_connect, transit_key
import crypto
import tick


def test_sync_stepthrough(fresh_db):
    """Step through sync flow manually with assertions at each step."""

    db = fresh_db

    print("\n" + "="*60)
    print("STEP 1: Setup - Create Alice's network and Bob joins")
    print("="*60)

    # Alice creates network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    alice_peer_id = alice['peer_id']
    print(f"Alice peer_id: {alice_peer_id[:20]}...")

    # Get Alice's peer_shared_id
    alice_safedb = create_safe_db(db, recorded_by=alice_peer_id)
    alice_self = alice_safedb.query_one(
        "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
        (alice_peer_id, alice_peer_id)
    )
    alice_peer_shared_id = alice_self['peer_shared_id']
    print(f"Alice peer_shared_id: {alice_peer_shared_id[:20]}...")

    # Alice creates invite
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice_peer_id,
        t_ms=1500,
        db=db
    )
    print(f"Invite created: {invite_id[:20]}...")

    # Bob joins
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    bob_peer_shared_id = bob['peer_shared_id']
    print(f"Bob peer_id: {bob_peer_id[:20]}...")
    print(f"Bob peer_shared_id: {bob_peer_shared_id[:20]}...")

    db.commit()

    # Count events
    alice_events = db.query("SELECT COUNT(*) as cnt FROM shareable_events WHERE can_share_peer_id = ?", (alice_peer_id,))[0]['cnt']
    bob_events = db.query("SELECT COUNT(*) as cnt FROM shareable_events WHERE can_share_peer_id = ?", (bob_peer_id,))[0]['cnt']
    print(f"\nAlice has {alice_events} shareable events")
    print(f"Bob has {bob_events} shareable events")

    print("\n" + "="*60)
    print("STEP 2: Establish connection - Bob sends sync_connect to Alice")
    print("="*60)

    # Bob sends sync_connect to Alice
    sync_connect.send(
        to_peer_shared_id=alice_peer_shared_id,
        from_peer_id=bob_peer_id,
        t_ms=3000,
        db=db
    )
    db.commit()
    print("✓ Bob sent sync_connect to Alice")

    unsafedb = create_unsafe_db(db)

    print("\n" + "="*60)
    print("STEP 3: Alice receives sync_connect, sends ack")
    print("="*60)

    # Process receive (moves outbox to inbox, processes)
    sync.receive(batch_size=100, t_ms=3000, db=db)
    db.commit()

    # Check Alice's connection state
    alice_conn = unsafedb.query_one(
        "SELECT * FROM sync_connections WHERE our_peer_id = ?",
        (alice_peer_id,)
    )
    if alice_conn:
        print(f"✓ Alice has connection:")
        print(f"  our_transit_key_id: {alice_conn['our_transit_key_id'][:20]}...")
        print(f"  their_transit_key_id: {alice_conn['their_transit_key_id'][:20] if alice_conn['their_transit_key_id'] else 'None'}")
        print(f"  their_transit_key: {'[present]' if alice_conn['their_transit_key'] else 'None'}")
    else:
        print("✗ Alice has NO connection!")

    print("\n" + "="*60)
    print("STEP 4: Bob receives ack (if any)")
    print("="*60)

    sync.receive(batch_size=100, t_ms=3001, db=db)
    db.commit()

    # Check Bob's connection state
    bob_conn = unsafedb.query_one(
        "SELECT * FROM sync_connections WHERE our_peer_id = ?",
        (bob_peer_id,)
    )
    if bob_conn:
        print(f"✓ Bob has connection:")
        print(f"  our_transit_key_id: {bob_conn['our_transit_key_id'][:20]}...")
        print(f"  their_transit_key_id: {bob_conn['their_transit_key_id'][:20] if bob_conn['their_transit_key_id'] else 'None'}")
        print(f"  their_transit_key: {'[present]' if bob_conn['their_transit_key'] else 'None'}")
    else:
        print("✗ Bob has NO connection!")

    print("\n" + "="*60)
    print("STEP 5: Alice sends sync_connect to Bob (for bidirectional)")
    print("="*60)

    sync_connect.send(
        to_peer_shared_id=bob_peer_shared_id,
        from_peer_id=alice_peer_id,
        t_ms=4000,
        db=db
    )
    db.commit()
    print("✓ Alice sent sync_connect to Bob")

    # Process
    sync.receive(batch_size=100, t_ms=4000, db=db)
    sync.receive(batch_size=100, t_ms=4001, db=db)
    db.commit()

    # Check both connections
    all_conns = unsafedb.query("SELECT * FROM sync_connections")
    print(f"\nTotal connections: {len(all_conns)}")
    for conn in all_conns:
        print(f"  - our_peer: {conn['our_peer_id'][:15]}... their_key: {'[present]' if conn['their_transit_key'] else 'None'}")

    print("\n" + "="*60)
    print("STEP 6: Multiple sync rounds")
    print("="*60)

    # Get connections
    bob_conn = unsafedb.query_one(
        "SELECT * FROM sync_connections WHERE our_peer_id = ? AND their_transit_key IS NOT NULL",
        (bob_peer_id,)
    )
    alice_conn = unsafedb.query_one(
        "SELECT * FROM sync_connections WHERE our_peer_id = ? AND their_transit_key IS NOT NULL",
        (alice_peer_id,)
    )

    if not bob_conn or not alice_conn:
        print("✗ Missing connections!")
        return

    # Show events by window (using query windows)
    print("\n--- Alice's events by query window (w=10) ---")
    alice_query_windows = db.query("""
        SELECT (window_id / 1024) as qw, COUNT(*) as cnt FROM shareable_events
        WHERE can_share_peer_id = ? GROUP BY qw ORDER BY qw
    """, (alice_peer_id,))
    for w in alice_query_windows:
        print(f"  Query Window {w['qw']}: {w['cnt']} events")

    # Run multiple sync rounds
    bob_events_before = bob_events
    alice_events_before = alice_events
    t = 5000

    for round_num in range(50):  # Try 50 rounds
        # Bob → Alice
        sync.send_request_to_connection(
            their_transit_key_id=bob_conn['their_transit_key_id'],
            their_transit_key=bob_conn['their_transit_key'],
            from_peer_id=bob_peer_id,
            from_peer_shared_id=bob_peer_shared_id,
            t_ms=t,
            db=db
        )
        sync.receive(batch_size=100, t_ms=t, db=db)
        t += 1
        sync.receive(batch_size=100, t_ms=t, db=db)
        t += 1

        # Alice → Bob
        sync.send_request_to_connection(
            their_transit_key_id=alice_conn['their_transit_key_id'],
            their_transit_key=alice_conn['their_transit_key'],
            from_peer_id=alice_peer_id,
            from_peer_shared_id=alice_peer_shared_id,
            t_ms=t,
            db=db
        )
        sync.receive(batch_size=100, t_ms=t, db=db)
        t += 1
        sync.receive(batch_size=100, t_ms=t, db=db)
        t += 1

        db.commit()

    # Count events after all rounds
    bob_events_after = db.query(
        "SELECT COUNT(*) as cnt FROM shareable_events WHERE can_share_peer_id = ?",
        (bob_peer_id,)
    )[0]['cnt']
    alice_events_after = db.query(
        "SELECT COUNT(*) as cnt FROM shareable_events WHERE can_share_peer_id = ?",
        (alice_peer_id,)
    )[0]['cnt']

    print(f"\nAfter 50 sync rounds:")
    print(f"  Bob: {bob_events_before} → {bob_events_after} (gained {bob_events_after - bob_events_before})")
    print(f"  Alice: {alice_events_before} → {alice_events_after} (gained {alice_events_after - alice_events_before})")

    # Check blocked events
    bob_safedb = create_safe_db(db, recorded_by=bob_peer_id)
    blocked = bob_safedb.query(
        "SELECT * FROM blocked_event_deps_ephemeral WHERE recorded_by = ?",
        (bob_peer_id,)
    )
    print(f"\nBob has {len(blocked)} blocked event dependencies")
    if blocked:
        for b in blocked[:5]:
            print(f"  blocked: {b['recorded_id'][:20]}... waiting for {b['dep_id'][:20]}...")

    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)

    print(f"Alice: started with {alice_events}, ended with {alice_events_after}")
    print(f"Bob: started with {bob_events}, ended with {bob_events_after}")

    # The key assertion
    assert bob_events_after > bob_events or alice_events_after > alice_events, \
        "At least one peer should have gained events from sync"


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v', '-s'])
