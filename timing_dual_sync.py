#!/usr/bin/env python3
"""Check if both sync mechanisms are running simultaneously."""

import time
import os
import sys
import sqlite3

import logging
# Enable logging to see what's happening
logging.basicConfig(level=logging.WARNING, format='%(name)s: %(message)s')

from db import Database, create_unsafe_db
import schema
import tick
import jobs
from events.identity import user, invite, peer
from events.content import message, message_attachment

def run_test():
    """Profile sync behavior with logging."""

    print("=== Dual Sync Check ===\n")

    # Initialize database
    conn = sqlite3.connect(':memory:')
    db = Database(conn)
    schema.create_all(db)
    db.commit()

    # Create users
    alice = user.new_network(name='alice', t_ms=1000, db=db)
    bob_peer_id = peer.create(t_ms=2000, db=db)
    invite_id, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='bob', t_ms=2000, db=db)
    db.commit()

    # Initial sync
    t_ms = 3000
    for i in range(20):
        tick.tick(t_ms, db)
        t_ms += 100
    db.commit()

    # Send image
    print("--- Sending Image ---")
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Check out this image!',
        t_ms=t_ms,
        db=db
    )
    t_ms += 100

    file_data = os.urandom(200 * 1024)
    file_result = message_attachment.create(
        peer_id=alice['peer_id'],
        message_id=msg_result['id'],
        file_data=file_data,
        filename='test-image.png',
        mime_type='image/png',
        t_ms=t_ms,
        db=db
    )
    db.commit()
    print(f"Image created: {file_result['slice_count']} slices")

    # Run a few ticks with verbose logging to see what's happening
    print("\n--- Running 5 ticks with logging ---")

    # Enable debug logging for sync modules
    import logging
    logging.getLogger('events.network.sync').setLevel(logging.WARNING)
    logging.getLogger('events.network.negentropy').setLevel(logging.WARNING)

    for i in range(5):
        t_ms += 100
        print(f"\n=== TICK {i} (t_ms={t_ms}) ===")
        tick.tick(t_ms, db)

    # Count what's in the queues
    unsafedb = create_unsafe_db(db)
    incoming = unsafedb.query_one("SELECT COUNT(*) as c FROM incoming_blobs")['c']
    sync_states = unsafedb._db._conn.execute("SELECT COUNT(*) FROM negentropy_sync_state").fetchone()[0]

    print(f"\n--- Results ---")
    print(f"Incoming blobs: {incoming}")
    print(f"Negentropy sync states: {sync_states}")

    # List the jobs
    print("\n--- Active Jobs ---")
    for job in jobs.JOBS:
        print(f"  {job.name}: every {job.every_ms}ms")


if __name__ == '__main__':
    run_test()
