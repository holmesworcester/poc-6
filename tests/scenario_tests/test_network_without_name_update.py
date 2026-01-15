"""
Scenario test: Network created without explicit name still displays properly.

This tests that removing the unused 'name' parameter from network.create()
doesn't break anything. Networks can be created without a name update event
and should still be queryable and usable.
"""
import sqlite3
import pytest
from core.db import Database
from core import schema
from events.identity import user
from events.content import message


def test_network_without_explicit_name():
    """Network created without explicit name update still works properly."""

    # Setup: Initialize in-memory database
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)

    # Create all tables using centralized schema loader
    schema.create_all(db)

    # Create network WITHOUT providing network_name parameter
    # This simulates a minimal network creation without any name update
    alice = user.new_network(
        name='Alice',
        t_ms=1000,
        db=db
        # Note: no network_name parameter
    )

    # Verify all core components were created
    assert len(alice['peer_id']) == 24
    assert len(alice['peer_shared_id']) == 24
    assert len(alice['network_id']) == 24
    assert len(alice['user_id']) == 24
    assert len(alice['group_id']) == 24
    assert len(alice['channel_id']) == 24

    # Verify network is queryable
    from events.identity import network
    all_users_group_id = network.get_all_users_group_id(
        alice['network_id'],
        alice['peer_id'],
        db
    )
    assert all_users_group_id == alice['group_id']

    # Verify user can send messages (full workflow)
    result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Hello from network without explicit name',
        t_ms=2000,
        db=db
    )
    assert 'id' in result
    assert result['latest'][0]['content'] == 'Hello from network without explicit name'

    # Verify messages are queryable
    messages = message.list(alice['channel_id'], alice['peer_id'], db)
    assert len(messages) == 1
    assert messages[0]['content'] == 'Hello from network without explicit name'

    # Verify groups are queryable
    from events.group import group
    groups_list = group.list_all_groups(alice['peer_id'], db)
    assert len(groups_list) == 1  # all_users group

    print("✓ Network without explicit name works correctly")

    # Re-projection test: verify we can restore from event store
    from tests.utils import assert_reprojection
    assert_reprojection(db)

    # Idempotency test: verify projection can be repeated without changing state
    from tests.utils import assert_idempotency
    assert_idempotency(db, num_trials=10, max_repetitions=5)

    # Convergence test: verify projection is order-independent
    from tests.utils import assert_convergence
    assert_convergence(db)


def test_network_with_explicit_name():
    """Network created WITH explicit name also works."""

    # Setup: Initialize in-memory database
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)

    # Create all tables
    schema.create_all(db)

    # Create network WITH network_name parameter
    alice = user.new_network(
        name='Alice',
        t_ms=1000,
        db=db,
        network_name='My Cool Network'  # Explicit network name
    )

    # Verify all core components were created
    assert len(alice['peer_id']) == 24
    assert len(alice['network_id']) == 24

    # Verify network is queryable
    from events.identity import network
    all_users_group_id = network.get_all_users_group_id(
        alice['network_id'],
        alice['peer_id'],
        db
    )
    assert all_users_group_id == alice['group_id']

    # Verify user can still send messages
    result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Hello from network with explicit name',
        t_ms=2000,
        db=db
    )
    assert 'id' in result
    assert result['latest'][0]['content'] == 'Hello from network with explicit name'

    print("✓ Network with explicit name also works correctly")

    # Re-projection test
    from tests.utils import assert_reprojection
    assert_reprojection(db)


if __name__ == '__main__':
    test_network_without_explicit_name()
    test_network_with_explicit_name()
