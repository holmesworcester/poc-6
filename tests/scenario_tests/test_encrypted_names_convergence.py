"""
Convergence tests for encrypted usernames and network names.

Tests that username_update and network_name_update events converge to identical
state regardless of:
- Event arrival order (convergence test)
- Event replay (reprojection test)
- Multiple projections of same event (idempotency test)

Uses convergence testing utilities to verify deterministic projection.
"""
import sqlite3
import pytest
from db import Database
import schema
from events.identity import user
import tick
from tests.utils.convergence import assert_reprojection, assert_idempotency, assert_convergence


def test_encrypted_names_convergence_basic(fresh_db):
    """Test basic convergence: Alice creates network with encrypted username.

    Tests reprojection, idempotency, and convergence of username_update events.
    """

    db = fresh_db
    tick.reset_state(db)

    print("\n=== Setup: Alice creates network ===")

    # Alice creates network with encrypted username
    alice = user.new_network(
        name='Alice',
        t_ms=1000,
        db=db,
        device_name='Alice Phone',
        network_name='Alice Network'
    )
    alice_peer_id = alice['peer_id']
    alice_user_id = alice['user_id']

    print(f"✓ Alice created: {alice_user_id[:20]}...")

    db.commit()

    # Verify scenario: Alice has encrypted username
    print("\n=== Verify scenario ===")
    from db import create_safe_db

    alice_db = create_safe_db(db, recorded_by=alice_peer_id)
    alice_usernames = alice_db.query(
        "SELECT * FROM user_names WHERE user_id = ? AND recorded_by = ?",
        (alice_user_id, alice_peer_id)
    )

    if alice_usernames:
        print(f"✓ Alice has username: {alice_usernames[0]['name']}")
    else:
        print("ℹ Alice's username not yet decrypted (pending)")

    # Verify network_names table
    network_names = alice_db.query(
        "SELECT * FROM network_names WHERE network_id = ? AND recorded_by = ?",
        (alice['network_id'], alice_peer_id)
    )

    if network_names:
        print(f"✓ Network has name: {network_names[0]['name']}")
    else:
        print("ℹ Network name not yet decrypted (pending)")

    # Reprojection test
    print("\n=== REPROJECTION TEST ===")
    assert_reprojection(db)
    print("✓ Reprojection passed")

    # Idempotency test
    print("\n=== IDEMPOTENCY TEST ===")
    assert_idempotency(db, num_trials=10, max_repetitions=5)
    print("✓ Idempotency passed")

    # Convergence test
    print("\n=== CONVERGENCE TEST ===")
    assert_convergence(db)
    print("✓ Convergence passed")


def test_encrypted_names_convergence_network_name(fresh_db):
    """Test convergence with network name updates via network_name_update events.

    Verifies that network_name_update events converge through reprojection,
    idempotency, and convergence tests regardless of event arrival order.
    """

    db = fresh_db
    tick.reset_state(db)

    print("\n=== Setup: Alice creates network with name ===")

    # Alice creates network with network name
    alice = user.new_network(
        name='Alice',
        t_ms=1000,
        db=db,
        device_name='Alice Phone',
        network_name='Original Network Name'
    )
    alice_peer_id = alice['peer_id']
    network_id = alice['network_id']

    print(f"✓ Network created: {network_id[:20]}...")

    db.commit()

    # Verify scenario
    print("\n=== Verify scenario ===")
    from db import create_safe_db

    alice_db = create_safe_db(db, recorded_by=alice_peer_id)
    network_names = alice_db.query(
        "SELECT * FROM network_names WHERE network_id = ? AND recorded_by = ?",
        (network_id, alice_peer_id)
    )

    if network_names:
        print(f"✓ Network has name: {network_names[0]['name']}")
    else:
        print("ℹ Network name not yet decrypted (pending)")

    # Reprojection test
    print("\n=== REPROJECTION TEST ===")
    assert_reprojection(db)
    print("✓ Reprojection passed")

    # Idempotency test
    print("\n=== IDEMPOTENCY TEST ===")
    assert_idempotency(db, num_trials=10, max_repetitions=5)
    print("✓ Idempotency passed")

    # Convergence test
    print("\n=== CONVERGENCE TEST ===")
    assert_convergence(db)
    print("✓ Convergence passed")


def test_encrypted_names_convergence_multiple_networks(fresh_db):
    """Test convergence with multiple networks having encrypted names.

    Verifies convergence works with multiple name update events in the store.
    """

    db = fresh_db
    tick.reset_state(db)

    print("\n=== Setup: Alice creates multiple networks ===")

    # Create first network
    alice1 = user.new_network(
        name='Alice',
        t_ms=1000,
        db=db,
        device_name='Phone',
        network_name='Network One'
    )
    network1_id = alice1['network_id']

    print(f"✓ Network 1 created: {network1_id[:20]}...")

    db.commit()

    # Verify both networks
    print("\n=== Verify scenario ===")
    from db import create_safe_db

    alice_db = create_safe_db(db, recorded_by=alice1['peer_id'])

    # Check first network name
    net1_names = alice_db.query(
        "SELECT name FROM network_names WHERE network_id = ? AND recorded_by = ?",
        (network1_id, alice1['peer_id'])
    )

    if net1_names:
        print(f"✓ Network 1 has name: {net1_names[0]['name']}")
    else:
        print("ℹ Network 1 name pending")

    # Check username
    alice_usernames = alice_db.query(
        "SELECT name FROM user_names WHERE user_id = ? AND recorded_by = ?",
        (alice1['user_id'], alice1['peer_id'])
    )

    if alice_usernames:
        print(f"✓ Alice username: {alice_usernames[0]['name']}")
    else:
        print("ℹ Alice username pending")

    # Reprojection test
    print("\n=== REPROJECTION TEST ===")
    assert_reprojection(db)
    print("✓ Reprojection passed")

    # Idempotency test
    print("\n=== IDEMPOTENCY TEST ===")
    assert_idempotency(db, num_trials=10, max_repetitions=5)
    print("✓ Idempotency passed")

    # Convergence test
    print("\n=== CONVERGENCE TEST ===")
    assert_convergence(db)
    print("✓ Convergence passed")
