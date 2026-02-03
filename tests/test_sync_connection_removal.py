"""Unit tests for connection removal functions."""
import pytest
from core.db import create_safe_db
from events.identity import user, peer, invite
from events.network import connection_request
from tests.utils import tick_helper
from tests.utils.tick_helper import TestClock


# Use global fresh_db fixture from conftest.py (sets up transport simulator)


def test_remove_connections_for_peer(fresh_db):
    """Test removing connections for a specific peer."""
    db = fresh_db
    clock = TestClock()

    # Create Alice and Bob in a network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db, device_name='Phone')
    db.commit()

    invite_id, link_url, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db,
        mode='user'
    )
    db.commit()

    bob = user.join(
        peer_id=peer.create(t_ms=clock.tick(), db=db),
        invite_link=link_url,
        name='Bob',
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Wait for connections to establish
    def has_connections():
        safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        conns = list(safedb.query(
            "SELECT * FROM connections WHERE recorded_by = ?",
            (alice['peer_id'],)
        ))
        assert len(conns) > 0, f"Expected connections, got {len(conns)}"
        return True

    tick_helper.assert_eventually(has_connections, db=db)

    # Get connections count for verification
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    connections_before = list(safedb.query(
        "SELECT * FROM connections WHERE recorded_by = ?",
        (alice['peer_id'],)
    ))
    print(f"Connections before removal: {len(connections_before)}")

    # Get Bob's peer_shared_id
    bob_peer_shared_id = bob['peer_shared_id']

    # Remove connections for Bob
    deleted_count = connection_request.remove_connections_for_peer(bob_peer_shared_id, db)
    db.commit()
    print(f"Deleted {deleted_count} connections for Bob")

    # Check that Bob's connections are gone (from Alice's perspective)
    remaining_connections = list(safedb.query(
        "SELECT * FROM connections WHERE peer_shared_id = ? AND recorded_by = ?",
        (bob_peer_shared_id, alice['peer_id'])
    ))
    assert len(remaining_connections) == 0, "Bob's connections should be deleted"


def test_remove_connections_for_user(fresh_db):
    """Test removing connections for all peers of a user."""
    db = fresh_db
    clock = TestClock()

    # Create Alice and Bob in a network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db, device_name='Phone')
    db.commit()

    invite_id, link_url, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db,
        mode='user'
    )
    db.commit()

    bob = user.join(
        peer_id=peer.create(t_ms=clock.tick(), db=db),
        invite_link=link_url,
        name='Bob',
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Wait for connections to establish
    def has_connections():
        safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        conns = list(safedb.query(
            "SELECT * FROM connections WHERE recorded_by = ?",
            (alice['peer_id'],)
        ))
        assert len(conns) > 0, f"Expected connections, got {len(conns)}"
        return True

    tick_helper.assert_eventually(has_connections, db=db)

    # Get connections count for verification
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    connections_before = list(safedb.query(
        "SELECT * FROM connections WHERE recorded_by = ?",
        (alice['peer_id'],)
    ))
    print(f"Connections before removal: {len(connections_before)}")

    # Get Bob's user_id
    bob_user_id = bob['user_id']

    # Remove connections for Bob's user
    deleted_count = connection_request.remove_connections_for_user(bob_user_id, db)
    db.commit()
    print(f"Deleted {deleted_count} connections for user")

    # Verify connections to Bob are gone
    bob_peers = list(safedb.query(
        "SELECT peer_shared_id FROM peers_shared WHERE user_id = ? AND recorded_by = ?",
        (bob_user_id, alice['peer_id'])
    ))

    for peer_row in bob_peers:
        bob_peer_shared_id = peer_row['peer_shared_id']
        remaining_connections = list(safedb.query(
            "SELECT * FROM connections WHERE peer_shared_id = ? AND recorded_by = ?",
            (bob_peer_shared_id, alice['peer_id'])
        ))
        assert len(remaining_connections) == 0, f"Connections to Bob's peer should be deleted"


def test_remove_connections_no_transit_keys(fresh_db):
    """Test removing connections when peer has no connections."""
    db = fresh_db
    clock = TestClock()

    # Create Alice
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db, device_name='Phone')
    db.commit()

    # Try to remove connections for a non-existent peer
    deleted_count = connection_request.remove_connections_for_peer("nonexistent_peer_id", db)
    assert deleted_count == 0, "Should return 0 when no connections found"

    # Try to remove connections for a non-existent user
    deleted_count = connection_request.remove_connections_for_user("nonexistent_user_id", db)
    assert deleted_count == 0, "Should return 0 when no connections found"


def test_user_removal_deletes_connections(fresh_db):
    """Scenario test: user removal should delete their connections."""
    db = fresh_db
    clock = TestClock()
    from events.identity import user_removed

    print("\n=== Setup: Create network with Alice and Bob ===")

    # Create Alice's network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db, device_name='Phone')
    db.commit()
    print(f"Alice created network, peer_id: {alice['peer_id'][:20]}...")

    # Bob joins the network
    invite_id, link_url, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db,
        mode='user'
    )
    db.commit()

    bob = user.join(
        peer_id=peer.create(t_ms=clock.tick(), db=db),
        invite_link=link_url,
        name='Bob',
        t_ms=clock.tick(),
        db=db
    )
    db.commit()
    print(f"Bob joined network, user_id: {bob['user_id'][:20]}...")

    # Wait for connections to establish
    print("\n=== Initial sync to establish connections ===")

    def has_connections():
        safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        conns = list(safedb.query(
            "SELECT * FROM connections WHERE recorded_by = ?",
            (alice['peer_id'],)
        ))
        assert len(conns) > 0, f"Expected connections, got {len(conns)}"
        return True

    tick_helper.assert_eventually(has_connections, db=db)

    # Check connections before removal
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    connections_before = list(safedb.query(
        "SELECT * FROM connections WHERE recorded_by = ?",
        (alice['peer_id'],)
    ))
    print(f"Connections before removal: {len(connections_before)}")

    # Count connections to Bob before removal
    bob_connections_before = list(safedb.query(
        "SELECT * FROM connections WHERE peer_shared_id = ? AND recorded_by = ?",
        (bob['peer_shared_id'], alice['peer_id'])
    ))
    print(f"Connections to Bob before removal: {len(bob_connections_before)}")

    # Remove Bob
    print("\n=== Alice removes Bob ===")
    user_removed.create(
        removed_user_id=bob['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db
    )
    db.commit()
    print("Bob removed")

    # Verify connections to Bob are deleted
    bob_connections_after = list(safedb.query(
        "SELECT * FROM connections WHERE peer_shared_id = ? AND recorded_by = ?",
        (bob['peer_shared_id'], alice['peer_id'])
    ))
    print(f"Connections to Bob after removal: {len(bob_connections_after)}")
    assert len(bob_connections_after) == 0, "Connections to removed user should be deleted"

    # Verify Bob is in removed_users table
    removed_users = list(safedb.query(
        "SELECT * FROM removed_users WHERE user_id = ? AND recorded_by = ?",
        (bob['user_id'], alice['peer_id'])
    ))
    assert len(removed_users) > 0, "Bob should be in removed_users table"
    print("✓ Bob's connections deleted and marked as removed")


def test_remove_connections_for_invite(fresh_db):
    """Test removing bootstrap connections by invite_id."""
    db = fresh_db
    clock = TestClock()

    # Create Alice's network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db, device_name='Phone')
    db.commit()

    # Create invite
    invite_id, link_url, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db,
        mode='user'
    )
    db.commit()

    # Bob joins - this creates bootstrap connections with invite_id
    bob = user.join(
        peer_id=peer.create(t_ms=clock.tick(), db=db),
        invite_link=link_url,
        name='Bob',
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Run a few ticks to establish bootstrap connections
    def has_bootstrap_connections():
        safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        conns = list(safedb.query(
            "SELECT * FROM connections WHERE invite_id = ? AND recorded_by = ?",
            (invite_id, alice['peer_id'])
        ))
        assert len(conns) > 0, f"Expected bootstrap connections, got {len(conns)}"
        return True

    tick_helper.assert_eventually(has_bootstrap_connections, db=db)

    # Count bootstrap connections before removal
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    bootstrap_before = list(safedb.query(
        "SELECT * FROM connections WHERE invite_id = ? AND recorded_by = ?",
        (invite_id, alice['peer_id'])
    ))
    assert len(bootstrap_before) > 0, "Should have bootstrap connections"

    # Remove connections for this invite
    deleted_count = connection_request.remove_connections_for_invite(invite_id, db)
    db.commit()

    assert deleted_count > 0, "Should have deleted bootstrap connections"

    # Verify bootstrap connections are gone
    bootstrap_after = list(safedb.query(
        "SELECT * FROM connections WHERE invite_id = ? AND recorded_by = ?",
        (invite_id, alice['peer_id'])
    ))
    assert len(bootstrap_after) == 0, "Bootstrap connections should be deleted"


def test_send_to_all_skips_removed_peers(fresh_db):
    """Test that send_to_all doesn't create connections to removed peers."""
    db = fresh_db
    clock = TestClock()
    from events.identity import user_removed
    from core.db import create_unsafe_db

    # Create Alice's network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db, device_name='Phone')
    db.commit()

    # Bob joins
    invite_id, link_url, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db,
        mode='user'
    )
    db.commit()

    bob = user.join(
        peer_id=peer.create(t_ms=clock.tick(), db=db),
        invite_link=link_url,
        name='Bob',
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Wait for Bob to be synced to Alice's peers_shared (required for removal cascade)
    def bob_synced():
        safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        bob_peer = safedb.query_one(
            "SELECT 1 FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ?",
            (bob['peer_shared_id'], alice['peer_id'])
        )
        assert bob_peer is not None, "Bob should be in Alice's peers_shared"
        return True

    tick_helper.assert_eventually(bob_synced, db=db)

    # Remove Bob
    user_removed.create(
        removed_user_id=bob['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Verify Bob is in removed_peers
    unsafedb = create_unsafe_db(db)
    removed = unsafedb.query_one(
        "SELECT 1 FROM removed_peers WHERE peer_shared_id = ?",
        (bob['peer_shared_id'],)
    )
    assert removed is not None, "Bob should be in removed_peers"

    # Verify no connections to Bob exist
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    bob_connections = list(safedb.query(
        "SELECT * FROM connections WHERE peer_shared_id = ? AND recorded_by = ?",
        (bob['peer_shared_id'], alice['peer_id'])
    ))
    assert len(bob_connections) == 0, "No connections to Bob after removal"

    # Call send_to_all - should NOT create new connections to Bob
    t_ms = clock.tick()
    connection_request.send_to_all(t_ms=t_ms, db=db)
    db.commit()

    # Verify still no connections to Bob
    bob_connections_after = list(safedb.query(
        "SELECT * FROM connections WHERE peer_shared_id = ? AND recorded_by = ?",
        (bob['peer_shared_id'], alice['peer_id'])
    ))
    assert len(bob_connections_after) == 0, "send_to_all should not create connections to removed peers"


def test_user_removal_invalidates_bootstrap_connections(fresh_db):
    """Test that user removal deletes bootstrap connections via invite invalidation."""
    db = fresh_db
    clock = TestClock()
    from events.identity import user_removed

    # Create Alice's network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db, device_name='Phone')
    db.commit()

    # Create invite
    invite_id, link_url, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db,
        mode='user'
    )
    db.commit()

    # Bob joins
    bob = user.join(
        peer_id=peer.create(t_ms=clock.tick(), db=db),
        invite_link=link_url,
        name='Bob',
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Run a few ticks - this creates bootstrap connections
    def has_bootstrap_connections():
        safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        conns = list(safedb.query(
            "SELECT * FROM connections WHERE invite_id IS NOT NULL AND recorded_by = ?",
            (alice['peer_id'],)
        ))
        assert len(conns) > 0, f"Expected bootstrap connections, got {len(conns)}"
        return True

    tick_helper.assert_eventually(has_bootstrap_connections, db=db)

    # Count all connections (bootstrap + direct)
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    all_connections_before = list(safedb.query(
        "SELECT * FROM connections WHERE recorded_by = ?",
        (alice['peer_id'],)
    ))
    bootstrap_before = [c for c in all_connections_before if c['invite_id'] is not None]
    assert len(bootstrap_before) > 0, "Should have bootstrap connections before removal"

    # Remove Bob - this should delete both direct AND bootstrap connections
    user_removed.create(
        removed_user_id=bob['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Verify ALL connections are gone (direct by peer_shared_id, bootstrap by invite)
    all_connections_after = list(safedb.query(
        "SELECT * FROM connections WHERE recorded_by = ?",
        (alice['peer_id'],)
    ))
    assert len(all_connections_after) == 0, \
        f"All connections should be deleted after removal, got {len(all_connections_after)}"


def test_connections_not_recreated_after_removal(fresh_db):
    """Regression test: connections should not be recreated after user removal."""
    db = fresh_db
    clock = TestClock()
    from events.identity import user_removed

    # Create Alice's network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db, device_name='Phone')
    db.commit()

    # Bob joins
    invite_id, link_url, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db,
        mode='user'
    )
    db.commit()

    bob = user.join(
        peer_id=peer.create(t_ms=clock.tick(), db=db),
        invite_link=link_url,
        name='Bob',
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Wait for connections
    def has_connections():
        safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        conns = list(safedb.query(
            "SELECT * FROM connections WHERE recorded_by = ?",
            (alice['peer_id'],)
        ))
        assert len(conns) > 0, f"Expected connections, got {len(conns)}"
        return True

    tick_helper.assert_eventually(has_connections, db=db)

    # Remove Bob
    user_removed.create(
        removed_user_id=bob['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Verify no connections after removal
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    connections_after_removal = list(safedb.query(
        "SELECT * FROM connections WHERE recorded_by = ?",
        (alice['peer_id'],)
    ))
    assert len(connections_after_removal) == 0, "No connections after removal"

    # Run multiple ticks - connections should NOT be recreated
    for _ in range(10):
        tick_helper.run_ticks(db=db, start_t_ms=None, num_rounds=1)

    # Verify still no connections
    connections_after_ticks = list(safedb.query(
        "SELECT * FROM connections WHERE recorded_by = ?",
        (alice['peer_id'],)
    ))
    assert len(connections_after_ticks) == 0, \
        f"Connections should not be recreated after removal, got {len(connections_after_ticks)}"
