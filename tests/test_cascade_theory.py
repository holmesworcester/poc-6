"""Test to verify/disprove the O(N²) cascade theory.

Theory: The old notify_event_valid() returned ALL events with deps_remaining=0,
causing O(N²) redundant processing during recursive projection.

This test:
1. Creates a multi-player scenario with many events
2. Counts how many times projection is called with old vs new behavior
3. Measures actual timing
"""
import sqlite3
import time
from core.db import Database, create_safe_db
from core import schema, queues
from events.identity import user, invite, peer


def simulate_old_vs_new():
    """Simulate what the OLD buggy behavior would do vs CURRENT."""

    print("\n=== Setting up test scenario ===")

    conn = sqlite3.connect(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)

    # Add Bob and Charlie to create more events
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    charlie_peer_id = peer.create(t_ms=3000, db=db)
    charlie = user.join(peer_id=charlie_peer_id, invite_link=invite_link, name='Charlie', t_ms=3000, db=db)

    db.commit()

    # Count events created
    store_count = db.query("SELECT COUNT(*) as c FROM store")[0]['c']
    valid_count = db.query("SELECT COUNT(*) as c FROM valid_events")[0]['c']
    blocked_count = db.query("SELECT COUNT(*) as c FROM blocked_events_ephemeral")[0]['c']

    print(f"Events in store: {store_count}")
    print(f"Valid events: {valid_count}")
    print(f"Blocked events: {blocked_count}")

    # Now simulate what would happen with OLD behavior
    # The OLD code did this in notify_event_valid:
    #   all_ready = query("SELECT * FROM blocked_events WHERE deps_remaining = 0")
    #   return newly_unblocked | all_ready

    # Let's manually trace through what would happen
    print("\n=== Simulating cascade behavior ===")

    # Create a fresh scenario to trace
    conn2 = sqlite3.connect(":memory:")
    db2 = Database(conn2)
    schema.create_all(db2)

    # Manually track what notify_event_valid returns
    notify_calls_old = []
    notify_calls_new = []

    # Instrument the function
    original_notify = queues.blocked.notify_event_valid

    def tracking_notify_new(event_id, recorded_by, safedb):
        result = original_notify(event_id, recorded_by, safedb)
        notify_calls_new.append({
            'event': event_id[:20],
            'returned': len(result),
            'ids': [r[:12] for r in result[:5]]
        })
        return result

    def tracking_notify_old(event_id, recorded_by, safedb):
        # First get what current behavior returns
        newly_unblocked = original_notify(event_id, recorded_by, safedb)

        # Then add what OLD behavior would add
        all_ready = safedb.query("""
            SELECT recorded_id FROM blocked_events_ephemeral
            WHERE deps_remaining = 0 AND recorded_by = ?
        """, (recorded_by,))
        all_ready_ids = [row['recorded_id'] for row in all_ready]

        combined = list(set(newly_unblocked) | set(all_ready_ids))

        notify_calls_old.append({
            'event': event_id[:20],
            'newly': len(newly_unblocked),
            'all_ready': len(all_ready_ids),
            'combined': len(combined)
        })

        return combined

    # Test with NEW (current) behavior
    queues.blocked.notify_event_valid = tracking_notify_new

    alice2 = user.new_network(name='Alice', t_ms=1000, db=db2)
    invite_id2, invite_link2, _ = invite.create(peer_id=alice2['peer_id'], t_ms=1500, db=db2)
    db2.commit()

    bob_peer_id2 = peer.create(t_ms=2000, db=db2)
    bob2 = user.join(peer_id=bob_peer_id2, invite_link=invite_link2, name='Bob', t_ms=2000, db=db2)
    db2.commit()

    print(f"\nNEW behavior - notify_event_valid called {len(notify_calls_new)} times")
    total_new = sum(c['returned'] for c in notify_calls_new)
    print(f"Total events returned across all calls: {total_new}")

    # Now test with OLD behavior simulation
    conn3 = sqlite3.connect(":memory:")
    db3 = Database(conn3)
    schema.create_all(db3)

    notify_calls_old = []
    queues.blocked.notify_event_valid = tracking_notify_old

    alice3 = user.new_network(name='Alice', t_ms=1000, db=db3)
    invite_id3, invite_link3, _ = invite.create(peer_id=alice3['peer_id'], t_ms=1500, db=db3)
    db3.commit()

    bob_peer_id3 = peer.create(t_ms=2000, db=db3)
    bob3 = user.join(peer_id=bob_peer_id3, invite_link=invite_link3, name='Bob', t_ms=2000, db=db3)
    db3.commit()

    print(f"\nOLD behavior - notify_event_valid called {len(notify_calls_old)} times")
    total_old = sum(c['combined'] for c in notify_calls_old)
    total_newly = sum(c['newly'] for c in notify_calls_old)
    total_all_ready = sum(c['all_ready'] for c in notify_calls_old)
    print(f"Total events returned (combined): {total_old}")
    print(f"  - newly unblocked: {total_newly}")
    print(f"  - all_ready additions: {total_all_ready - total_newly}")

    # Show calls where all_ready added extra
    extras = [c for c in notify_calls_old if c['all_ready'] > c['newly']]
    if extras:
        print(f"\nCalls where OLD behavior returned extra events:")
        for c in extras[:10]:
            print(f"  newly={c['newly']}, all_ready={c['all_ready']}, combined={c['combined']}")
    else:
        print(f"\nNo calls where OLD behavior would return extra events!")

    # Restore original
    queues.blocked.notify_event_valid = original_notify

    print(f"\n=== COMPARISON ===")
    print(f"NEW behavior: {len(notify_calls_new)} calls, {total_new} events returned")
    print(f"OLD behavior: {len(notify_calls_old)} calls, {total_old} events returned")

    if total_old > total_new:
        print(f"OLD returned {total_old - total_new} more events ({total_old/max(total_new,1):.1f}x)")
        print(f"OLD made {len(notify_calls_old) - len(notify_calls_new)} more calls ({len(notify_calls_old)/max(len(notify_calls_new),1):.1f}x)")
    else:
        print(f"NO DIFFERENCE - theory does not hold for this scenario")

    # Now do timing comparison with actual recursive projection
    print(f"\n=== TIMING COMPARISON ===")

    # Time NEW behavior
    conn4 = sqlite3.connect(":memory:")
    db4 = Database(conn4)
    schema.create_all(db4)
    queues.blocked.notify_event_valid = original_notify  # Use current behavior

    start = time.time()
    alice4 = user.new_network(name='Alice', t_ms=1000, db=db4)
    invite_id4, invite_link4, _ = invite.create(peer_id=alice4['peer_id'], t_ms=1500, db=db4)
    bob_peer_id4 = peer.create(t_ms=2000, db=db4)
    bob4 = user.join(peer_id=bob_peer_id4, invite_link=invite_link4, name='Bob', t_ms=2000, db=db4)
    charlie_peer_id4 = peer.create(t_ms=3000, db=db4)
    charlie4 = user.join(peer_id=charlie_peer_id4, invite_link=invite_link4, name='Charlie', t_ms=3000, db=db4)
    db4.commit()
    new_time = time.time() - start

    # Time OLD behavior (simulated)
    conn5 = sqlite3.connect(":memory:")
    db5 = Database(conn5)
    schema.create_all(db5)
    queues.blocked.notify_event_valid = tracking_notify_old  # Use old behavior

    notify_calls_old = []  # Reset
    start = time.time()
    alice5 = user.new_network(name='Alice', t_ms=1000, db=db5)
    invite_id5, invite_link5, _ = invite.create(peer_id=alice5['peer_id'], t_ms=1500, db=db5)
    bob_peer_id5 = peer.create(t_ms=2000, db=db5)
    bob5 = user.join(peer_id=bob_peer_id5, invite_link=invite_link5, name='Bob', t_ms=2000, db=db5)
    charlie_peer_id5 = peer.create(t_ms=3000, db=db5)
    charlie5 = user.join(peer_id=charlie_peer_id5, invite_link=invite_link5, name='Charlie', t_ms=3000, db=db5)
    db5.commit()
    old_time = time.time() - start

    queues.blocked.notify_event_valid = original_notify

    print(f"NEW behavior time: {new_time:.3f}s")
    print(f"OLD behavior time: {old_time:.3f}s")
    print(f"Slowdown factor: {old_time/max(new_time, 0.001):.1f}x")

    # Scale test - add more peers
    print(f"\n=== SCALING TEST (more peers) ===")

    for num_joiners in [5, 10]:
        # NEW behavior
        conn_new = sqlite3.connect(":memory:")
        db_new = Database(conn_new)
        schema.create_all(db_new)

        start = time.time()
        alice_n = user.new_network(name='Alice', t_ms=1000, db=db_new)
        invite_id_n, invite_link_n, _ = invite.create(peer_id=alice_n['peer_id'], t_ms=1500, db=db_new)

        for i in range(num_joiners):
            joiner_peer_id = peer.create(t_ms=2000 + i*1000, db=db_new)
            joiner = user.join(peer_id=joiner_peer_id, invite_link=invite_link_n, name=f'User{i}', t_ms=2000 + i*1000, db=db_new)
        db_new.commit()
        new_time_scaled = time.time() - start

        # OLD behavior
        conn_old = sqlite3.connect(":memory:")
        db_old = Database(conn_old)
        schema.create_all(db_old)
        queues.blocked.notify_event_valid = tracking_notify_old
        notify_calls_old = []

        start = time.time()
        alice_o = user.new_network(name='Alice', t_ms=1000, db=db_old)
        invite_id_o, invite_link_o, _ = invite.create(peer_id=alice_o['peer_id'], t_ms=1500, db=db_old)

        for i in range(num_joiners):
            joiner_peer_id = peer.create(t_ms=2000 + i*1000, db=db_old)
            joiner = user.join(peer_id=joiner_peer_id, invite_link=invite_link_o, name=f'User{i}', t_ms=2000 + i*1000, db=db_old)
        db_old.commit()
        old_time_scaled = time.time() - start

        queues.blocked.notify_event_valid = original_notify

        print(f"{num_joiners} joiners: NEW={new_time_scaled:.3f}s, OLD={old_time_scaled:.3f}s, slowdown={old_time_scaled/max(new_time_scaled, 0.001):.1f}x, calls={len(notify_calls_old)}")


if __name__ == '__main__':
    simulate_old_vs_new()
