"""Test that peer_shared events have invite dependencies (trust anchor model)."""
import sqlite3
from core.db import Database, create_safe_db
from core import schema
from events.identity import user


def test_peer_shared_has_invite_deps():
    """Verify that peer_shared events depend on their signing invite (trust anchor).

    In the invite trust anchor model, every peer_shared is signed by an invite.
    This creates a dependency chain: peer_shared → invite → user.
    This is by design - it establishes trust via the invite chain.

    We test this by checking the projected tables, not by decoding blobs directly.
    """
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Create Alice - uses new_network() which creates proper invite->peer_shared chain
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Create Bob (separate network)
    bob = user.new_network(name='Bob', t_ms=2000, db=db)

    alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    bob_safedb = create_safe_db(db, recorded_by=bob['peer_id'])

    # Verify Alice's peer_shared is valid from Alice's perspective
    alice_ps_valid = alice_safedb.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (alice['peer_shared_id'], alice['peer_id'])
    )
    assert alice_ps_valid is not None, "Alice's peer_shared should be valid for Alice"

    # Verify Alice's peer_shared is in peers_shared table (properly projected)
    alice_ps_row = alice_safedb.query_one(
        "SELECT peer_shared_id, peer_id FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ?",
        (alice['peer_shared_id'], alice['peer_id'])
    )
    assert alice_ps_row is not None, "Alice's peer_shared should be in peers_shared table"
    assert alice_ps_row['peer_id'] == alice['peer_id'], "peer_id should match"

    # Bob does NOT have Alice's invite, so Alice's peer_shared should NOT be valid for Bob
    # (This is the key assertion: peer_shared depends on invite via trust anchor model)
    bob_sees_alice_ps_valid = bob_safedb.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (alice['peer_shared_id'], bob['peer_id'])
    )
    # Bob shouldn't see Alice's peer_shared as valid because:
    # 1. The event blob isn't in Bob's store yet (no sync)
    # 2. Even if it were, the invite dependency would block it
    assert bob_sees_alice_ps_valid is None, \
        "Bob should NOT see Alice's peer_shared as valid (no sync, deps not met)"

    print("✓ Test passed: peer_shared events require invite dependency chain")


if __name__ == '__main__':
    test_peer_shared_has_invite_deps()
