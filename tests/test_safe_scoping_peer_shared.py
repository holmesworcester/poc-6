"""Test that peer_shared events have no dependencies when received by foreign peers."""
import sqlite3
from db import Database
import schema
from events.identity import user, peer_shared
from events.transit import recorded
import store


def test_peer_shared_has_no_foreign_deps():
    """Verify that Alice's peer_shared event is valid for Bob (no deps)."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Create Alice
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Create Bob (separate network for simplicity)
    bob = user.new_network(name='Bob', t_ms=2000, db=db)

    # Get Alice's peer_shared event data
    alice_ps_blob = store.get(alice['peer_shared_id'], db)
    alice_ps_data = eval(alice_ps_blob.decode())  # It's signed JSON

    print(f"Alice's peer_shared event: {alice_ps_data}")
    print(f"Alice's peer_id: {alice['peer_id']}")
    print(f"Bob's peer_id: {bob['peer_id']}")

    # Check deps from Bob's perspective
    missing_deps = recorded.check_deps(alice_ps_data, bob['peer_id'], db)

    print(f"Missing deps for Bob receiving Alice's peer_shared: {missing_deps}")

    # Should have NO missing dependencies
    assert len(missing_deps) == 0, f"peer_shared should have no foreign deps, but found: {missing_deps}"

    print("✓ Test passed: peer_shared events have no dependencies when received by foreign peers")


if __name__ == '__main__':
    test_peer_shared_has_no_foreign_deps()
