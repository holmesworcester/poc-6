"""Unit tests for event reprojection."""
import sqlite3
import pytest
from db import Database
import schema
from events.identity import user, invite
from tests.utils.convergence import assert_reprojection


def test_alice_network_reprojection():
    """Test that Alice's network setup is idempotent under reprojection."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    assert alice['peer_id'] is not None

    # Test reprojection
    assert_reprojection(db)


def test_alice_and_bob_reprojection():
    """Test reprojection with Alice and Bob joining."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Alice creates network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Create invite
    invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)

    # Bob joins
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    assert bob['peer_id'] is not None

    # Test reprojection
    assert_reprojection(db)
