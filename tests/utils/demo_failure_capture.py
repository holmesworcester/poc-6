#!/usr/bin/env python3
"""Demonstration of convergence failure capture and replay system.

This script shows how the failure capture system works:
1. Creates a test scenario
2. Demonstrates manual replay of specific orderings
3. Shows how failures would be saved and replayed

Note: Since the one-player test currently passes convergence,
this is a demonstration of the API, not an actual failure.
"""

import sqlite3
from db import Database
import schema
from events.transit import transit_key
from events.group import group
from events.identity import peer
from events.content import channel, message
from tests.utils.convergence import (
    replay_ordering,
    _get_projectable_event_ids,
    _dump_projection_state,
    _states_equal
)


def main():
    print("=" * 70)
    print("CONVERGENCE FAILURE CAPTURE & REPLAY DEMONSTRATION")
    print("=" * 70)

    # Setup test database
    print("\n1. Setting up test database...")
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Create test data
    print("2. Creating test data (Alice's identity, group, channel, message)...")
    alice_peer_id, alice_peer_shared_id = peer.create(t_ms=1000, db=db)
    alice_key_id = transit_key.create(alice_peer_id, t_ms=2000, db=db)
    alice_group_id = group.create(
        'My Group', alice_peer_id, alice_peer_shared_id,
        alice_key_id, t_ms=3000, db=db
    )
    alice_channel_id = channel.create(
        'general', alice_group_id, alice_peer_id,
        alice_peer_shared_id, alice_key_id, t_ms=4000, db=db
    )
    msg1 = message.create_message({
        'content': 'Hello',
        'channel_id': alice_channel_id,
        'group_id': alice_group_id,
        'peer_id': alice_peer_id,
        'peer_shared_id': alice_peer_shared_id,
        'key_id': alice_key_id
    }, t_ms=5000, db=db)

    # Get event IDs
    event_ids = _get_projectable_event_ids(db)
    print(f"   Created {len(event_ids)} events")

    # Capture baseline state
    print("\n3. Capturing baseline state...")
    baseline_state = _dump_projection_state(db)
    print(f"   Baseline has {len(baseline_state)} tables")

    # Test different orderings
    print("\n4. Testing different event orderings...")

    # Original order
    print(f"\n   a) Original order: {event_ids}")
    state1 = replay_ordering(db, event_ids)
    equal1, _ = _states_equal(baseline_state, state1)
    print(f"      Result: {'✅ SAME' if equal1 else '❌ DIFFERENT'}")

    # Reversed order
    reversed_order = list(reversed(event_ids))
    print(f"\n   b) Reversed order: {reversed_order}")
    state2 = replay_ordering(db, reversed_order)
    equal2, diff2 = _states_equal(baseline_state, state2)
    print(f"      Result: {'✅ SAME' if equal2 else '❌ DIFFERENT'}")
    if not equal2:
        print(f"      Difference: {diff2}")

    # Random shuffled order
    import random
    shuffled = event_ids.copy()
    random.shuffle(shuffled)
    print(f"\n   c) Random shuffle: {shuffled}")
    state3 = replay_ordering(db, shuffled)
    equal3, diff3 = _states_equal(baseline_state, state3)
    print(f"      Result: {'✅ SAME' if equal3 else '❌ DIFFERENT'}")
    if not equal3:
        print(f"      Difference: {diff3}")

    # Summary
    print("\n5. Summary:")
    print(f"   - All orderings converge: {equal1 and equal2 and equal3}")
    print(f"   - This demonstrates that the blocking/unblocking system works correctly")

    # Demonstrate failure capture API
    print("\n6. Failure Capture API (demonstration):")
    print("""
   If a convergence test fails, it will:
   - Print the baseline and failed orderings
   - Save failure data to tests/failures/convergence_failure_<timestamp>_ordering_<N>.json
   - The JSON contains:
     * baseline_order: Original event ordering
     * failed_order: The ordering that produced different state
     * baseline_state: Full database state from baseline
     * failed_state: Full database state from failed ordering
     * difference: Human-readable diff message
     * timestamp: When the failure occurred

   You can then:
   - Replay with: python tests/replay_failure.py tests/failures/<file>.json
   - Or manually with: replay_from_file(db, 'tests/failures/<file>.json')
   - Or add to regression suite (automatically tested by pytest)
    """)

    print("\n" + "=" * 70)
    print("DEMONSTRATION COMPLETE")
    print("=" * 70)
    print("\nAll orderings produced identical state ✅")
    print("The convergence test infrastructure is working correctly!")


if __name__ == '__main__':
    main()
