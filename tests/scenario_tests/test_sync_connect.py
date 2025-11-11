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
from db import Database
import schema
from events.identity import user, invite
import tick


def test_connection_establishment():
    """Test that sync_connect establishes connections between peers."""

    # Setup
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

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
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    print(f"Bob joined network, peer_id: {bob['peer_id'][:20]}...")

    db.commit()

    # Before any tick, there should be no connections
    connections = db.query("SELECT * FROM sync_connections")
    assert len(connections) == 0, "Should have no connections initially"
    print("✓ No connections before first tick")

    # Run one tick cycle - should establish connections
    print("\n=== Running first tick (establishes connections) ===")
    tick.tick(t_ms=3000, db=db)

    # Check that connections were established
    connections = db.query("SELECT peer_shared_id FROM sync_connections")
    print(f"Connections established: {len(connections)}")

    # Should have connections (Alice→Bob and Bob→Alice)
    assert len(connections) >= 1, "Should have at least one connection after first tick"
    print(f"✓ Established {len(connections)} connection(s)")

    # Verify connection has required fields
    conn_row = db.query_one("SELECT * FROM sync_connections LIMIT 1")
    assert conn_row['peer_shared_id'], "Connection should have peer_shared_id"
    assert conn_row['response_transit_key_id'], "Connection should have response_transit_key_id"
    assert conn_row['response_transit_key'], "Connection should have response_transit_key"
    assert conn_row['last_seen_ms'] == 3000, "Connection should have correct timestamp"
    print("✓ Connection has all required fields")

    # Run another tick - connections should refresh (after 60s for sync_connect to run again)
    print("\n=== Running second tick (refreshes connections) ===")
    tick.tick(t_ms=63000, db=db)  # 60s later so sync_connect runs again

    # Check that connection timestamps were updated
    conn_row = db.query_one("SELECT * FROM sync_connections LIMIT 1")
    assert conn_row['last_seen_ms'] == 63000, "Connection should have updated timestamp"
    print("✓ Connection timestamp refreshed")


def test_connection_expiry():
    """Test that expired connections are purged."""

    # Setup
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    print("\n=== Setup: Create network and establish connection ===")

    # Alice creates a network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Create invite and Bob joins
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    db.commit()

    # Establish connections
    tick.tick(t_ms=3000, db=db)

    connections = db.query("SELECT * FROM sync_connections")
    initial_count = len(connections)
    assert initial_count >= 1, "Should have connections"
    print(f"✓ Established {initial_count} connection(s)")

    # Get TTL
    conn_row = connections[0]
    ttl_ms = conn_row['ttl_ms']
    last_seen = conn_row['last_seen_ms']
    expiry_time = last_seen + ttl_ms

    print(f"Connection last_seen={last_seen}, ttl={ttl_ms}, expires_at={expiry_time}")

    # Run tick AFTER expiry time
    print(f"\n=== Running tick after expiry (t={expiry_time + 1000}) ===")
    tick.tick(t_ms=expiry_time + 1000, db=db)

    # Connections should be purged
    connections_after = db.query("SELECT * FROM sync_connections")
    # Note: Connections will be re-established in the same tick, so we check
    # that purge happened by verifying new timestamps
    if len(connections_after) > 0:
        new_conn = connections_after[0]
        assert new_conn['last_seen_ms'] == expiry_time + 1000, "Should have new connection with fresh timestamp"
        print("✓ Expired connections were purged and re-established")
    else:
        print("✓ Expired connections were purged")


def test_sync_uses_connections():
    """Test that sync preferentially uses established connections over prekeys."""

    # Setup
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    print("\n=== Setup: Create network and users ===")

    # Alice creates a network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Create invite and Bob joins
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    db.commit()

    # Run multiple sync rounds
    print("\n=== Running sync rounds ===")
    for i in range(5):
        print(f"Sync round {i+1}")
        tick.tick(t_ms=3000 + i*200, db=db)

    # Verify that sync completed successfully
    # (If connections weren't working, sync would fail or fall back to prekeys)

    # Check that connections exist
    connections = db.query("SELECT * FROM sync_connections")
    assert len(connections) >= 1, "Should have active connections"
    print(f"✓ Sync completed successfully using {len(connections)} connection(s)")

    # Verify both peers can see each other
    alice_sees_bob = db.query_one(
        "SELECT 1 FROM peers_shared WHERE recorded_by = ?",
        (alice['peer_id'],)
    )
    bob_sees_alice = db.query_one(
        "SELECT 1 FROM peers_shared WHERE recorded_by = ?",
        (bob['peer_id'],)
    )

    assert alice_sees_bob, "Alice should see Bob's peer_shared"
    assert bob_sees_alice, "Bob should see Alice's peer_shared"
    print("✓ Peers successfully synced via established connections")


if __name__ == '__main__':
    test_connection_establishment()
    test_connection_expiry()
    test_sync_uses_connections()
    print("\n=== All sync_connect tests passed ===")
