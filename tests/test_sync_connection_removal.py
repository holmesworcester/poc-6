"""Unit tests for connection removal functions."""
import pytest
import sqlite3
from core.db import Database, create_safe_db
from core import schema
from events.identity import user, peer, invite
from events.network import connection
from tests.utils import tick_helper


@pytest.fixture
def fresh_db():
    """Create a fresh in-memory database."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)
    return db


def test_remove_connections_for_peer(fresh_db):
    """Test removing connections for a specific peer."""
    db = fresh_db

    # Create Alice and Bob in a network
    alice = user.new_network(name='Alice', t_ms=1000, db=db, device_name='Phone')
    db.commit()

    invite_id, link_url, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=2000,
        db=db,
        mode='user'
    )
    db.commit()

    bob = user.join(
        peer_id=peer.create(t_ms=2500, db=db),
        invite_link=link_url,
        name='Bob',
        t_ms=3000,
        db=db
    )
    db.commit()

    # Run sync to establish connections
    tick_helper.run_ticks(db=db, start_t_ms=4000, num_rounds=50)
    db.commit()

    # Check that connections exist
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    connections_before = safedb.query(
        "SELECT * FROM connections WHERE recorded_by = ?",
        (alice['peer_id'],)
    )
    print(f"Connections before removal: {len(connections_before)}")
    assert len(connections_before) > 0, "Should have connections after sync"

    # Get Bob's peer_shared_id
    bob_peer_shared_id = bob['peer_shared_id']

    # Remove connections for Bob
    deleted_count = connection.remove_connections_for_peer(bob_peer_shared_id, db)
    db.commit()
    print(f"Deleted {deleted_count} connections for Bob")

    # Check that Bob's connections are gone (from Alice's perspective)
    remaining_connections = safedb.query(
        "SELECT * FROM connections WHERE peer_shared_id = ? AND recorded_by = ?",
        (bob_peer_shared_id, alice['peer_id'])
    )
    assert len(remaining_connections) == 0, "Bob's connections should be deleted"


def test_remove_connections_for_user(fresh_db):
    """Test removing connections for all peers of a user."""
    db = fresh_db

    # Create Alice and Bob in a network
    alice = user.new_network(name='Alice', t_ms=1000, db=db, device_name='Phone')
    db.commit()

    invite_id, link_url, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=2000,
        db=db,
        mode='user'
    )
    db.commit()

    bob = user.join(
        peer_id=peer.create(t_ms=2500, db=db),
        invite_link=link_url,
        name='Bob',
        t_ms=3000,
        db=db
    )
    db.commit()

    # Run sync to establish connections
    tick_helper.run_ticks(db=db, start_t_ms=4000, num_rounds=50)
    db.commit()

    # Check that connections exist
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    connections_before = safedb.query(
        "SELECT * FROM connections WHERE recorded_by = ?",
        (alice['peer_id'],)
    )
    print(f"Connections before removal: {len(connections_before)}")
    assert len(connections_before) > 0, "Should have connections after sync"

    # Get Bob's user_id
    bob_user_id = bob['user_id']

    # Remove connections for Bob's user
    deleted_count = connection.remove_connections_for_user(bob_user_id, db)
    db.commit()
    print(f"Deleted {deleted_count} connections for user")

    # Verify connections to Bob are gone
    # Find Bob's peer_shared_ids
    bob_peers = safedb.query(
        "SELECT peer_shared_id FROM peers_shared WHERE user_id = ? AND recorded_by = ?",
        (bob_user_id, alice['peer_id'])
    )

    for peer_row in bob_peers:
        bob_peer_shared_id = peer_row['peer_shared_id']
        remaining_connections = safedb.query(
            "SELECT * FROM connections WHERE peer_shared_id = ? AND recorded_by = ?",
            (bob_peer_shared_id, alice['peer_id'])
        )
        assert len(remaining_connections) == 0, f"Connections to Bob's peer {bob_peer_shared_id[:20]}... should be deleted"


def test_remove_connections_no_transit_keys(fresh_db):
    """Test removing connections when peer has no connections."""
    db = fresh_db

    # Create Alice
    alice = user.new_network(name='Alice', t_ms=1000, db=db, device_name='Phone')
    db.commit()

    # Try to remove connections for a non-existent peer
    deleted_count = connection.remove_connections_for_peer("nonexistent_peer_id", db)
    assert deleted_count == 0, "Should return 0 when no connections found"

    # Try to remove connections for a non-existent user
    deleted_count = connection.remove_connections_for_user("nonexistent_user_id", db)
    assert deleted_count == 0, "Should return 0 when no connections found"


def test_user_removal_deletes_connections(fresh_db):
    """Scenario test: user removal should delete their connections."""
    db = fresh_db
    from events.identity import user_removed

    print("\n=== Setup: Create network with Alice and Bob ===")

    # Create Alice's network
    alice = user.new_network(name='Alice', t_ms=1000, db=db, device_name='Phone')
    db.commit()
    print(f"Alice created network, peer_id: {alice['peer_id'][:20]}...")

    # Bob joins the network
    invite_id, link_url, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=2000,
        db=db,
        mode='user'
    )
    db.commit()

    bob = user.join(
        peer_id=peer.create(t_ms=2500, db=db),
        invite_link=link_url,
        name='Bob',
        t_ms=3000,
        db=db
    )
    db.commit()
    print(f"Bob joined network, user_id: {bob['user_id'][:20]}...")

    # Run sync to establish connections
    print("\n=== Initial sync to establish connections ===")
    tick_helper.run_ticks(db=db, start_t_ms=4000, num_rounds=100)
    db.commit()

    # Check connections before removal
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    connections_before = safedb.query(
        "SELECT * FROM connections WHERE recorded_by = ?",
        (alice['peer_id'],)
    )
    print(f"Connections before removal: {len(connections_before)}")
    assert len(connections_before) > 0, "Should have connections after sync"

    # Count connections to Bob before removal
    bob_connections_before = safedb.query(
        "SELECT * FROM connections WHERE peer_shared_id = ? AND recorded_by = ?",
        (bob['peer_shared_id'], alice['peer_id'])
    )
    print(f"Connections to Bob before removal: {len(bob_connections_before)}")

    # Remove Bob
    print("\n=== Alice removes Bob ===")
    user_removed.create(
        removed_user_id=bob['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=5000,
        db=db
    )
    db.commit()
    print("Bob removed")

    # Verify connections to Bob are deleted
    bob_connections_after = safedb.query(
        "SELECT * FROM connections WHERE peer_shared_id = ? AND recorded_by = ?",
        (bob['peer_shared_id'], alice['peer_id'])
    )
    print(f"Connections to Bob after removal: {len(bob_connections_after)}")
    assert len(bob_connections_after) == 0, "Connections to removed user should be deleted"

    # Verify Bob is in removed_users table
    removed_users = safedb.query(
        "SELECT * FROM removed_users WHERE user_id = ? AND recorded_by = ?",
        (bob['user_id'], alice['peer_id'])
    )
    assert len(removed_users) > 0, "Bob should be in removed_users table"
    print("✓ Bob's connections deleted and marked as removed")
