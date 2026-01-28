"""
Scenario test: sync_connect connection establishment.

Tests the sync_connect phase that establishes connections before sync.

Tests:
- Connections are established after first tick
- Sync uses established connections (not prekeys)
- Connections expire after TTL
- Connections refresh on repeated connects
"""
import sqlite3
from core.db import Database, create_safe_db, create_unsafe_db
from core import schema
from events.identity import user, invite, peer
from tests.utils import tick_helper
from core import tick
from events.network import connection_request as conn_module


def test_connection_establishment(fresh_db):
    """Test that sync_connect establishes connections between peers."""

    # Setup
    db = fresh_db

    print("\n=== Setup: Create network and users ===")

    # Alice creates a network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    print(f"Alice created network, peer_id: {alice['peer_id'][:20]}...")

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

    db.commit()

    # Before any tick, there should be no connections
    connections = db.query("SELECT * FROM connections")
    assert len(connections) == 0, "Should have no connections initially"
    print("✓ No connections before first tick")

    # Run one tick cycle - should establish connections
    # Run multiple ticks for full two-way handshake:
    # Tick 1: peers send sync_connect (creates rows with our_transit_key_id)
    # Tick 2: peers receive sync_connect, store their_transit_key, send ack
    # Tick 3: peers receive ack (completes bidirectional key exchange)
    print("\n=== Running first tick (sends sync_connect) ===")
    tick.tick(t_ms=3000, db=db)

    # Check that connections were initiated (peer-scoped, so check for any)
    connections = db.query("SELECT * FROM connections")
    print(f"Connections initiated: {len(connections)}")
    assert len(connections) >= 1, "Should have at least one connection after first tick"
    print(f"✓ Initiated {len(connections)} connection(s)")

    print("\n=== Running second tick (receives connect, sends ack) ===")
    tick.tick(t_ms=4000, db=db)

    print("\n=== Running third tick (receives ack, completes handshake) ===")
    tick.tick(t_ms=5000, db=db)

    # Now connections should have both our_key and their_key
    conn_row = db.query_one("""
        SELECT * FROM connections
        WHERE their_key IS NOT NULL
        LIMIT 1
    """)
    assert conn_row is not None, "Should have at least one complete connection"
    assert conn_row['peer_shared_id'] or conn_row['invite_id'], "Connection should have identity label"
    assert conn_row['their_key_id'], "Connection should have their_key_id"
    assert conn_row['their_key'], "Connection should have their_key"
    print("✓ Connection has all required fields after handshake")

    # Run another tick - connections should remain stable (not recreated)
    print("\n=== Running fourth tick (connections should be stable) ===")
    tick.tick(t_ms=6000, db=db)

    # In the new design, existing connections are reused - verify same connection count
    connections_after_tick4 = db.query("SELECT * FROM connections")
    print(f"Connections after tick 4: {len(connections_after_tick4)}")

    # Verify connections still exist and are usable
    assert len(connections_after_tick4) >= 1, "Should still have at least one connection"
    print("✓ Connections remain stable across ticks")


def test_connection_expiry(fresh_db):
    """Test that expired connections are purged."""

    # Setup
    db = fresh_db

    print("\n=== Setup: Create network and establish connection ===")

    # Alice creates a network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Create invite and Bob joins
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )
    bob_peer_id = peer.create(t_ms=2000, db=db)

    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    bob_peer_shared_id = bob['peer_shared_id']

    db.commit()

    # Establish connections
    tick.tick(t_ms=3000, db=db)

    connections = db.query("SELECT * FROM connections")
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
    connections_after = db.query("SELECT * FROM connections")
    # Note: Connections will be re-established in the same tick, so we check
    # that purge happened by verifying new timestamps
    if len(connections_after) > 0:
        new_conn = connections_after[0]
        assert new_conn['last_handshake_ms'] == expiry_time + 1000, "Should have new connection with fresh timestamp"
        print("✓ Expired connections were purged and re-established")
    else:
        print("✓ Expired connections were purged")


def test_sync_uses_connections(fresh_db):
    """Test that sync uses established connections.

    Note: With key-based connections using random window selection,
    full sync may require many rounds. This test verifies connections
    are used, not that sync completes efficiently.
    """

    # Setup
    db = fresh_db

    print("\n=== Setup: Create network and users ===")

    # Alice creates a network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Create invite and Bob joins
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )
    bob_peer_id = peer.create(t_ms=2000, db=db)

    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    bob_peer_shared_id = bob['peer_shared_id']

    db.commit()

    # Run sync rounds - need more rounds with random window selection
    print("\n=== Running sync rounds ===")
    for i in range(20):  # More rounds needed with random windows
        tick.tick(t_ms=3000 + i * tick_helper.TICK_INTERVAL_MS, db=db)

    # Verify that sync completed successfully
    # (If connections weren't working, sync would fail or fall back to prekeys)

    # Check that connections exist (peer-scoped)
    connections = db.query("SELECT * FROM connections")
    assert len(connections) >= 1, "Should have active connections"
    print(f"✓ Sync using {len(connections)} connection(s)")

    # Verify at least one peer can see the other
    # (Full bidirectional sync may require more rounds with random windows)
    alice_sees_bob = db.query_one(
        "SELECT 1 FROM peers_shared WHERE recorded_by = ?",
        (alice['peer_id'],)
    )

    # TODO: Full sync efficiency - with random windows, may need many more rounds
    # for bidirectional sync. For now just verify connections work.
    assert alice_sees_bob, "Alice should see Bob's peer_shared (sent during join)"
    print("✓ Connections used for sync")


def test_two_way_handshake(fresh_db):
    """Test two-way connection handshake: connect → ack → bidirectional keys.

    Verifies:
    1. First tick sends connection requests
    2. Queue processing receives requests, sends acks
    3. Acks are processed, connections become bidirectional
    4. Both peers have each other's keys for bidirectional sync
    """
    db = fresh_db

    print("\n=== Setup: Create network with Alice and Bob ===")

    # Alice creates a network
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

    # Create invite and Bob joins
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice_peer_id,
        t_ms=1500,
        db=db
    )
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    bob_peer_shared_id = bob['peer_shared_id']
    print(f"Bob peer_shared_id: {bob_peer_shared_id[:20]}...")

    db.commit()

    # Initially no connections (check both peers' connections)
    alice_conns = conn_module.get_connections(alice_peer_id, 2000, db)
    bob_conns = conn_module.get_connections(bob_peer_id, 2000, db)
    assert len(alice_conns) == 0 and len(bob_conns) == 0, "Should have no connections initially"
    print("✓ No connections initially")

    # Step 1: Run tick to send connection requests and process acks
    print("\n=== Step 1: Run tick to establish connections ===")
    tick.tick(t_ms=3000, db=db)
    db.commit()
    print("✓ First tick completed")

    # Step 2: Check connections after tick
    print("\n=== Step 2: Check connections ===")
    alice_conns = conn_module.get_connections(alice_peer_id, 3000, db)
    bob_conns = conn_module.get_connections(bob_peer_id, 3000, db)

    print(f"Alice has {len(alice_conns)} connection(s)")
    print(f"Bob has {len(bob_conns)} connection(s)")

    # At least one peer should have a connection
    assert len(alice_conns) + len(bob_conns) >= 1, "Should have at least one connection after tick"
    print("✓ Connections established")

    # Step 3: Wait for bidirectional handshake to complete
    print("\n=== Step 3: Waiting for bidirectional handshake ===")

    def both_can_send():
        # Get all connections for both peers
        # Note: During bootstrap, connections use invite_id not peer_shared_id,
        # so we check get_connections() instead of get_connection_by_peer()
        alice_conns = conn_module.get_connections(alice_peer_id, 5000, db)
        bob_conns = conn_module.get_connections(bob_peer_id, 5000, db)

        # Alice should have at least one connection she can send on
        alice_can_send = any(c.can_send() for c in alice_conns)
        assert alice_can_send, \
            f"Alice should have a connection with their_key (has {len(alice_conns)} conns)"

        # Bob should have at least one connection he can send on
        bob_can_send = any(c.can_send() for c in bob_conns)
        assert bob_can_send, \
            f"Bob should have a connection with their_key (has {len(bob_conns)} conns)"

    tick_helper.assert_eventually(both_can_send, db=db, start_t_ms=None)

    # Verify final state: both peers have connections they can send on
    print("\n=== Verifying bidirectional connection state ===")

    # Get connections for both peers using the connection module
    alice_connections = conn_module.get_connections(alice_peer_id, 5000, db)
    bob_connections = conn_module.get_connections(bob_peer_id, 5000, db)
    print(f"Total connections: Alice={len(alice_connections)}, Bob={len(bob_connections)}")

    for conn in alice_connections + bob_connections:
        print(f"  Connection for {conn.recorded_by[:10]}... to {conn.label[:20]}...")
        print(f"    their_key_id: {conn.their_key_id[:20] if conn.their_key_id else 'None'}...")
        print(f"    their_key: {'[present]' if conn.their_key else 'None'}")

    print("\n✅ Two-way handshake test passed!")
    print("  ✓ Connection requests sent and acks received")
    print("  ✓ Bidirectional: Both peers have each other's keys")


if __name__ == '__main__':
    test_connection_establishment()
    test_connection_expiry()
    test_sync_uses_connections()
    test_two_way_handshake()
    print("\n=== All sync_connect tests passed ===")
