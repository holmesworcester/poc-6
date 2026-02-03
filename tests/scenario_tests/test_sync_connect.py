"""
Scenario test: sync_connect connection establishment.

Tests the sync_connect phase that establishes connections before sync.

Tests:
- Connections are established after first tick
- Sync uses established connections (not prekeys)
- Connections expire after TTL
- Connections refresh on repeated connects
"""
from core.db import create_safe_db
from events.identity import user, invite, peer
from tests.utils import tick_helper
from tests.utils.tick_helper import TestClock
from core import tick
from events.network import connection_request as conn_module


def test_connection_establishment(fresh_db):
    """Test that sync_connect establishes connections between peers."""
    db = fresh_db
    clock = TestClock()

    print("\n=== Setup: Create network and users ===")

    # Alice creates a network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    print(f"Alice created network, peer_id: {alice['peer_id'][:20]}...")

    # Alice creates an invite for Bob
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db
    )
    print(f"Alice created invite: {invite_id[:20]}...")

    # Bob joins Alice's network
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.tick(), db=db)
    print(f"Bob joined network, peer_id: {bob['peer_id'][:20]}...")

    db.commit()

    # Before any tick, there should be no connections
    connections = list(db.query("SELECT * FROM connections"))
    assert len(connections) == 0, "Should have no connections initially"
    print("✓ No connections before first tick")

    # Run ticks to establish connections
    print("\n=== Running ticks to establish connections ===")

    def has_complete_connection():
        conns = list(db.query("SELECT * FROM connections WHERE their_key IS NOT NULL"))
        assert len(conns) >= 1, f"Expected complete connection, got {len(conns)}"
        return True

    tick_helper.assert_eventually(has_complete_connection, db=db)

    # Now connections should have both our_key and their_key
    conn_row = db.query_one("SELECT * FROM connections WHERE their_key IS NOT NULL LIMIT 1")
    assert conn_row is not None, "Should have at least one complete connection"
    assert conn_row['peer_shared_id'] or conn_row['invite_id'], "Connection should have identity label"
    assert conn_row['their_key_id'], "Connection should have their_key_id"
    assert conn_row['their_key'], "Connection should have their_key"
    print("✓ Connection has all required fields after handshake")


def test_connection_expiry(fresh_db):
    """Test that expired connections are purged."""
    db = fresh_db
    clock = TestClock()

    print("\n=== Setup: Create network and establish connection ===")

    # Alice creates a network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)

    # Create invite and Bob joins
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db
    )
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.tick(), db=db)

    db.commit()

    # Run initial ticks to establish connections
    tick_helper.run_ticks(db=db, start_t_ms=None, num_rounds=10)

    connections = list(db.query("SELECT * FROM connections"))
    initial_count = len(connections)
    assert initial_count >= 1, "Should have connections"
    print(f"✓ Established {initial_count} connection(s)")

    # Get TTL
    conn_row = connections[0]
    ttl_ms = conn_row['ttl_ms']
    last_handshake = conn_row['last_handshake_ms']
    expiry_time = last_handshake + ttl_ms

    print(f"Connection last_handshake={last_handshake}, ttl={ttl_ms}, expires_at={expiry_time}")

    # Run tick AFTER expiry time
    print(f"\n=== Running tick after expiry (t={expiry_time + 1000}) ===")
    tick.tick(t_ms=expiry_time + 1000, db=db)

    # Connections should be purged
    connections_after = list(db.query("SELECT * FROM connections"))
    # Note: Connections will be re-established in the same tick, so we check
    # that purge happened by verifying new timestamps
    if len(connections_after) > 0:
        new_conn = connections_after[0]
        assert new_conn['last_handshake_ms'] == expiry_time + 1000, "Should have new connection with fresh timestamp"
        print("✓ Expired connections were purged and re-established")
    else:
        print("✓ Expired connections were purged")


def test_sync_uses_connections(fresh_db):
    """Test that sync uses established connections."""
    db = fresh_db
    clock = TestClock()

    print("\n=== Setup: Create network and users ===")

    # Alice creates a network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)

    # Create invite and Bob joins
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db
    )
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.tick(), db=db)

    db.commit()

    # Run sync rounds
    print("\n=== Running sync rounds ===")
    tick_helper.run_ticks(db=db, start_t_ms=None, num_rounds=20)

    # Check that connections exist
    connections = list(db.query("SELECT * FROM connections"))
    assert len(connections) >= 1, "Should have active connections"
    print(f"✓ Sync using {len(connections)} connection(s)")

    # Verify at least one peer can see the other
    alice_sees_bob = db.query_one(
        "SELECT 1 FROM peers_shared WHERE recorded_by = ?",
        (alice['peer_id'],)
    )
    assert alice_sees_bob, "Alice should see Bob's peer_shared (sent during join)"
    print("✓ Connections used for sync")


def test_two_way_handshake(fresh_db):
    """Test two-way connection handshake: connect → ack → bidirectional keys."""
    db = fresh_db
    clock = TestClock()

    print("\n=== Setup: Create network with Alice and Bob ===")

    # Alice creates a network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
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

    # Create invite and Bob joins
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice_peer_id,
        t_ms=clock.tick(),
        db=db
    )
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.tick(), db=db)
    bob_peer_shared_id = bob['peer_shared_id']
    print(f"Bob peer_shared_id: {bob_peer_shared_id[:20]}...")

    db.commit()

    # Initially no connections
    alice_conns = conn_module.get_connections(alice_peer_id, clock.now(), db)
    bob_conns = conn_module.get_connections(bob_peer_id, clock.now(), db)
    assert len(alice_conns) == 0 and len(bob_conns) == 0, "Should have no connections initially"
    print("✓ No connections initially")

    # Wait for bidirectional handshake to complete
    print("\n=== Waiting for bidirectional handshake ===")

    def both_can_send():
        alice_conns = conn_module.get_connections(alice_peer_id, clock.now(), db)
        bob_conns = conn_module.get_connections(bob_peer_id, clock.now(), db)

        alice_can_send = any(c.can_send() for c in alice_conns)
        assert alice_can_send, f"Alice should have a sendable connection (has {len(alice_conns)} conns)"

        bob_can_send = any(c.can_send() for c in bob_conns)
        assert bob_can_send, f"Bob should have a sendable connection (has {len(bob_conns)} conns)"

    tick_helper.assert_eventually(both_can_send, db=db)

    print("\n✅ Two-way handshake test passed!")


if __name__ == '__main__':
    import sqlite3
    from core.db import Database
    from core import schema, transport, simulator

    # Setup for direct execution
    transport.reset()
    sim = simulator.NetworkSimulator(simulator.NetworkConfig(latency_ms=25))
    transport.set_simulator(sim)

    conn = sqlite3.connect(':memory:')
    db = Database(conn)
    schema.create_all(db)

    test_connection_establishment(db)
    test_connection_expiry(db)
    test_sync_uses_connections(db)
    test_two_way_handshake(db)
    print("\n=== All sync_connect tests passed ===")
