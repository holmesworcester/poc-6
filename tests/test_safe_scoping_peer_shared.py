"""Test that peer_shared events have invite dependencies (trust anchor model)."""
import sqlite3
from core.db import Database
from core import schema
from events.identity import user, peer_shared
from events.network import recorded
from core import store


def test_peer_shared_has_invite_deps():
    """Verify that peer_shared events depend on their signing invite (trust anchor).

    In the invite trust anchor model, every peer_shared is signed by an invite.
    This creates a dependency chain: peer_shared → invite → user.
    This is by design - it establishes trust via the invite chain.
    """
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

    # peer_shared SHOULD have invite_id and signed_by deps (trust anchor model)
    # This is expected - Bob needs to receive the invite first to validate the peer_shared
    invite_id = alice_ps_data.get('invite_id')
    signed_by = alice_ps_data.get('signed_by')

    assert invite_id is not None, "peer_shared should have invite_id (trust anchor)"
    assert signed_by == invite_id, "peer_shared should be signed by its invite"

    # If Bob doesn't have the invite yet, there should be missing deps
    # (This is the expected behavior - deps are resolved via sync)
    if missing_deps:
        print(f"✓ peer_shared correctly has invite deps: {missing_deps}")
        # Verify the missing dep is the invite_id
        assert invite_id in missing_deps, f"Missing dep should be the invite_id"
    else:
        # If no missing deps, it means the invite was already synced/available
        print("✓ peer_shared deps already satisfied (invite available)")

    print("✓ Test passed: peer_shared events have invite deps (trust anchor model)")


if __name__ == '__main__':
    test_peer_shared_has_invite_deps()
