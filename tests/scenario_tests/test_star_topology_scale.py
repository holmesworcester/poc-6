"""
Scaling tests for star topology sync.

These tests verify that star topology provides the expected scaling benefits:
- Mesh: O(n^2) negotiations per tick
- Star: O(n) negotiations per tick

The star topology improvement:
| N     | Mesh              | Star        | Improvement |
|-------|-------------------|-------------|-------------|
| 100   | 9,900 neg/tick    | 100 neg/tick| 99x         |
| 1,000 | 999,000 neg/tick  | 1,000 neg/tick | 999x     |
| 10,000| 99,990,000 neg/tick| 10,000 neg/tick | 9,999x |
"""
import pytest
from events.identity import user, invite, peer, network
from events.identity import server_relay, network_settings
from events.identity import peer_shared as peer_shared_module
from events.network import server_connection
from core.db import create_safe_db
from tests.utils.tick_helper import TestClock, run_ticks


def test_sync_targets_in_mesh_mode(fresh_db):
    """In mesh mode, get_sync_targets returns empty (sync with all)."""
    db = fresh_db
    clock = TestClock()

    # Setup: Alice creates network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Get network_id
    network_id = network.get_network_id(alice['peer_id'], db)

    # In mesh mode (default), get_sync_targets should return empty list
    # (empty means "use default mesh sync with all peers")
    targets = server_connection.get_sync_targets(
        peer_id=alice['peer_id'],
        network_id=network_id,
        db=db
    )

    assert targets == [], f"Mesh mode should return empty targets, got: {targets}"

    print("Mesh mode returns empty sync targets (sync with all)")


def test_sync_targets_in_star_mode_for_client(fresh_db):
    """In star mode, client returns only server relay as sync target."""
    from core import crypto
    db = fresh_db
    clock = TestClock()

    # Setup: Alice creates network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Get network_id and Alice's identity
    network_id = network.get_network_id(alice['peer_id'], db)
    alice_identity = peer_shared_module.get_self(alice['peer_id'], db)
    alice_peer_shared_id = alice_identity['peer_shared_id']

    # Set server relay (fake server for testing - use valid base64)
    # Generate a valid 16-byte ID encoded as base64
    server_peer_shared_id = crypto.b64encode(b"server_relay_123")
    server_address = "relay.example.com:5000"

    network_settings.set_server_relay(
        network_id=network_id,
        server_peer_shared_id=server_peer_shared_id,
        server_address=server_address,
        peer_id=alice['peer_id'],
        peer_shared_id=alice_peer_shared_id,
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Run ticks to project
    run_ticks(db=db, start_t_ms=clock.now(), num_rounds=5)

    # In star mode, Alice (client) should only sync with server
    targets = server_connection.get_sync_targets(
        peer_id=alice['peer_id'],
        network_id=network_id,
        db=db
    )

    assert targets == [server_peer_shared_id], \
        f"Star mode client should return server as target, got: {targets}"

    print("Star mode client returns server relay as sync target")


def test_should_sync_with_peer_star_topology(fresh_db):
    """should_sync_with_peer correctly filters based on star topology."""
    from core import crypto
    db = fresh_db
    clock = TestClock()

    # Setup: Alice creates network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Get network_id and Alice's identity
    network_id = network.get_network_id(alice['peer_id'], db)
    alice_identity = peer_shared_module.get_self(alice['peer_id'], db)
    alice_peer_shared_id = alice_identity['peer_shared_id']

    # Create Bob (another member)
    invite_id, invite_link, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db,
        mode='user'
    )
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.now(), db=db)
    db.commit()

    # Bob's peer_shared_id is returned directly from user.join()
    bob_peer_shared_id = bob['peer_shared_id']

    # In mesh mode (default), everyone syncs with everyone
    assert server_connection.should_sync_with_peer(
        our_peer_id=alice['peer_id'],
        our_peer_shared_id=alice_peer_shared_id,
        target_peer_shared_id=bob_peer_shared_id,
        network_id=network_id,
        db=db
    ), "Mesh mode: Alice should sync with Bob"

    # Set server relay (use valid base64)
    server_peer_shared_id = crypto.b64encode(b"fake_server_0123")
    network_settings.set_server_relay(
        network_id=network_id,
        server_peer_shared_id=server_peer_shared_id,
        server_address="relay.example.com:5000",
        peer_id=alice['peer_id'],
        peer_shared_id=alice_peer_shared_id,
        t_ms=clock.tick(),
        db=db
    )
    db.commit()
    run_ticks(db=db, start_t_ms=clock.now(), num_rounds=5)

    # In star mode, Alice should NOT sync directly with Bob
    assert not server_connection.should_sync_with_peer(
        our_peer_id=alice['peer_id'],
        our_peer_shared_id=alice_peer_shared_id,
        target_peer_shared_id=bob_peer_shared_id,
        network_id=network_id,
        db=db
    ), "Star mode: Alice should NOT sync with Bob directly"

    # Alice SHOULD sync with server relay
    assert server_connection.should_sync_with_peer(
        our_peer_id=alice['peer_id'],
        our_peer_shared_id=alice_peer_shared_id,
        target_peer_shared_id=server_peer_shared_id,
        network_id=network_id,
        db=db
    ), "Star mode: Alice should sync with server relay"

    print("Star topology correctly filters sync peers")


def test_star_topology_scaling_theory():
    """Verify the theoretical scaling improvement from star topology.

    This is a pure computation test - no actual sync happens.
    """
    def mesh_negotiations(n: int) -> int:
        """O(n^2) negotiations in mesh topology."""
        return n * (n - 1)  # Each peer syncs with every other peer

    def star_negotiations(n: int) -> int:
        """O(n) negotiations in star topology."""
        return n  # Each peer syncs only with server

    # Test cases from the specification
    test_cases = [
        (100, 9900, 100, 99),        # N=100: 99x improvement
        (1000, 999000, 1000, 999),   # N=1000: 999x improvement
        (10000, 99990000, 10000, 9999),  # N=10000: 9999x improvement
    ]

    for n, expected_mesh, expected_star, expected_improvement in test_cases:
        actual_mesh = mesh_negotiations(n)
        actual_star = star_negotiations(n)
        actual_improvement = actual_mesh // actual_star

        assert actual_mesh == expected_mesh, \
            f"N={n}: mesh should be {expected_mesh}, got {actual_mesh}"
        assert actual_star == expected_star, \
            f"N={n}: star should be {expected_star}, got {actual_star}"
        assert actual_improvement == expected_improvement, \
            f"N={n}: improvement should be {expected_improvement}x, got {actual_improvement}x"

        print(f"N={n:,}: Mesh={actual_mesh:,} neg/tick, Star={actual_star:,} neg/tick, "
              f"Improvement={actual_improvement:,}x")

    print("Star topology provides expected scaling improvements")


def test_star_vs_mesh_small_network(fresh_db):
    """Compare sync behavior in star vs mesh for a small network.

    This test creates a small network and verifies that:
    1. In mesh mode, each peer syncs with all others
    2. In star mode, each peer syncs only with server
    """
    db = fresh_db
    clock = TestClock()

    # Setup: Alice creates network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Get network info
    network_id = network.get_network_id(alice['peer_id'], db)
    alice_identity = peer_shared_module.get_self(alice['peer_id'], db)
    alice_peer_shared_id = alice_identity['peer_shared_id']

    # Add a few members
    members = [alice]
    for i in range(4):  # Add 4 more members (total 5)
        invite_id, invite_link, _ = invite.create(
            peer_id=alice['peer_id'],
            t_ms=clock.tick(),
            db=db,
            mode='user'
        )
        member_peer_id = peer.create(t_ms=clock.tick(), db=db)
        member = user.join(
            peer_id=member_peer_id,
            invite_link=invite_link,
            name=f'Member{i}',
            t_ms=clock.now(),
            db=db
        )
        members.append(member)
        db.commit()

    # In mesh mode, sync targets should be empty (sync with all)
    mesh_targets = server_connection.get_sync_targets(
        peer_id=alice['peer_id'],
        network_id=network_id,
        db=db
    )
    assert mesh_targets == [], "Mesh mode: empty targets (sync with all)"

    # Count theoretical syncs in mesh: each of 5 peers syncs with 4 others = 20 syncs
    mesh_syncs = len(members) * (len(members) - 1)
    print(f"Mesh mode: {len(members)} peers, {mesh_syncs} total sync pairs")

    # Switch to star mode (use valid base64)
    from core import crypto
    server_peer_shared_id = crypto.b64encode(b"fake_server_0123")
    network_settings.set_server_relay(
        network_id=network_id,
        server_peer_shared_id=server_peer_shared_id,
        server_address="relay.example.com:5000",
        peer_id=alice['peer_id'],
        peer_shared_id=alice_peer_shared_id,
        t_ms=clock.tick(),
        db=db
    )
    db.commit()
    run_ticks(db=db, start_t_ms=clock.now(), num_rounds=5)

    # In star mode, sync targets should be just the server
    star_targets = server_connection.get_sync_targets(
        peer_id=alice['peer_id'],
        network_id=network_id,
        db=db
    )
    assert star_targets == [server_peer_shared_id], "Star mode: only server as target"

    # Count theoretical syncs in star: each of 5 peers syncs with 1 server = 5 syncs
    star_syncs = len(members)
    improvement = mesh_syncs // star_syncs

    print(f"Star mode: {len(members)} peers, {star_syncs} total sync pairs")
    print(f"Improvement: {improvement}x fewer sync pairs")

    assert improvement == len(members) - 1, \
        f"Expected {len(members)-1}x improvement, got {improvement}x"

    print("Star topology reduces sync pairs as expected")


def test_sync_mode_defaults_to_mesh(fresh_db):
    """New networks default to mesh sync mode."""
    db = fresh_db
    clock = TestClock()

    # Setup: Alice creates network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Get network_id
    network_id = network.get_network_id(alice['peer_id'], db)

    # Verify default sync mode is mesh
    sync_mode = network_settings.get_sync_mode(network_id, alice['peer_id'], db)
    assert sync_mode == 'mesh', f"Default sync mode should be 'mesh', got '{sync_mode}'"

    # Verify no server relay is configured
    relay_info = network_settings.get_server_relay(network_id, alice['peer_id'], db)
    assert relay_info is None, "Default: no server relay configured"

    print("Networks default to mesh sync mode")


def test_server_relay_syncs_with_all_in_star_mode(fresh_db):
    """Server relay itself syncs with everyone in star mode.

    When the server relay checks should_sync_with_peer, it should
    return True for all peers (server syncs with everyone).
    """
    db = fresh_db
    clock = TestClock()

    # Setup: Alice creates network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Get network info
    network_id = network.get_network_id(alice['peer_id'], db)
    alice_identity = peer_shared_module.get_self(alice['peer_id'], db)
    alice_peer_shared_id = alice_identity['peer_shared_id']

    # Create Bob
    invite_id, invite_link, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db,
        mode='user'
    )
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.now(), db=db)
    db.commit()
    # Bob's peer_shared_id is returned directly from user.join()
    bob_peer_shared_id = bob['peer_shared_id']

    # Set Alice as the server relay (for testing purposes)
    network_settings.set_server_relay(
        network_id=network_id,
        server_peer_shared_id=alice_peer_shared_id,  # Alice is the server
        server_address="alice.example.com:5000",
        peer_id=alice['peer_id'],
        peer_shared_id=alice_peer_shared_id,
        t_ms=clock.tick(),
        db=db
    )
    db.commit()
    run_ticks(db=db, start_t_ms=clock.now(), num_rounds=5)

    # Alice (as server) should sync with Bob
    assert server_connection.should_sync_with_peer(
        our_peer_id=alice['peer_id'],
        our_peer_shared_id=alice_peer_shared_id,
        target_peer_shared_id=bob_peer_shared_id,
        network_id=network_id,
        db=db
    ), "Server relay should sync with all clients"

    # Bob (client) should only sync with Alice (server)
    assert server_connection.should_sync_with_peer(
        our_peer_id=bob['peer_id'],
        our_peer_shared_id=bob_peer_shared_id,
        target_peer_shared_id=alice_peer_shared_id,
        network_id=network_id,
        db=db
    ), "Client should sync with server"

    print("Server relay syncs with all peers in star mode")
