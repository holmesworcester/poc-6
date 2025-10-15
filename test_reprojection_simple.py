#!/usr/bin/env python3
"""Simple reprojection test with just Alice and Bob."""

import sqlite3
import logging
from db import Database
import schema
from events.identity import user, invite
from tests.utils.convergence import assert_reprojection

logging.basicConfig(
    level=logging.WARNING,
    format='%(levelname)-8s %(name)s:%(filename)s:%(lineno)d %(message)s'
)

conn = sqlite3.Connection(':memory:')
db = Database(conn)
schema.create_all(db)

# Alice creates network
alice = user.new_network(name='Alice', t_ms=1000, db=db)
print(f"Alice peer_id: {alice['peer_id'][:20]}...")

# Create invite
invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
print(f"Invite created: {invite_id[:20]}...")

# Bob joins
bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)
print(f"Bob peer_id: {bob['peer_id'][:20]}...")

# Test reprojection
print("\n=== Testing Reprojection ===")
assert_reprojection(db)
print("✓ Reprojection passed!")
