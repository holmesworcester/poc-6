"""Unit tests for reprojection with bootstrap events."""
import sqlite3
import pytest
from db import Database
import schema
from events.identity import user, invite
from events.transit import sync
from tests.utils.convergence import assert_reprojection


def test_minimal_reprojection_with_bootstrap():
    """Test reprojection with Alice and Bob after join."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Alice creates network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Create invite
    invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)

    # Bob joins
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    # Do sync rounds (minimal)
    for t in [4100, 4200]:
        sync.receive(batch_size=20, t_ms=t, db=db)

    # Test reprojection
    assert_reprojection(db)
