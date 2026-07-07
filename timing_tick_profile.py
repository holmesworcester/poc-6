#!/usr/bin/env python3
"""Profile the send tick in detail."""

import time
import os
import sqlite3
import cProfile
import pstats
from io import StringIO

import logging
logging.basicConfig(level=logging.CRITICAL)

from core.db import Database, create_unsafe_db
from core import schema
from core import tick
from core import transport
from events.identity import user, invite, peer
from events.content import message, message_attachment
from events.network import negentropy


def run_test():
    print("=== Tick Profile Analysis ===\n")

    transport.enable_loopback()
    transport.set_pacer(None, None)

    conn = sqlite3.connect(':memory:')
    db = Database(conn)
    schema.create_all(db)
    db.commit()

    base_t_ms = 1769500000000

    # Create users
    alice = user.new_network(name='alice', t_ms=base_t_ms, db=db)
    bob_peer_id = peer.create(t_ms=base_t_ms + 1000, db=db)
    invite_id, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=base_t_ms + 500, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='bob', t_ms=base_t_ms + 1000, db=db)
    db.commit()

    # Initial sync
    t_ms = base_t_ms + 2000
    for i in range(100):
        tick.tick(t_ms, db)
        t_ms += 100
    db.commit()

    # Create 1MB file
    print("Creating 1MB file...")
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='File',
        t_ms=t_ms,
        db=db
    )
    t_ms += 100

    file_data = os.urandom(1024 * 1024)
    file_result = message_attachment.create(
        peer_id=alice['peer_id'],
        message_id=msg_result['id'],
        file_data=file_data,
        filename='test.bin',
        mime_type='application/octet-stream',
        t_ms=t_ms,
        db=db
    )
    db.commit()
    print(f"Created {file_result['slice_count']} slices\n")

    # Run a few ticks to get to the send tick
    t_ms += 100
    for i in range(4):
        tick.tick(t_ms, db)
        t_ms += 100

    # Profile tick 4 (the send tick)
    print("--- Profiling tick 4 (send tick) ---\n")

    pr = cProfile.Profile()
    pr.enable()

    t0 = time.perf_counter()
    tick.tick(t_ms, db)
    elapsed = time.perf_counter() - t0

    pr.disable()

    print(f"Tick time: {elapsed*1000:.1f}ms\n")

    # Show top functions by cumulative time
    s = StringIO()
    ps = pstats.Stats(pr, stream=s).sort_stats('cumulative')
    ps.print_stats(30)
    print(s.getvalue())

    # Now profile the receive ticks
    t_ms += 100

    print("\n--- Profiling tick 5 (receive tick) ---\n")

    pr2 = cProfile.Profile()
    pr2.enable()

    t0 = time.perf_counter()
    tick.tick(t_ms, db)
    elapsed = time.perf_counter() - t0

    pr2.disable()

    print(f"Tick time: {elapsed*1000:.1f}ms\n")

    s2 = StringIO()
    ps2 = pstats.Stats(pr2, stream=s2).sort_stats('cumulative')
    ps2.print_stats(30)
    print(s2.getvalue())


if __name__ == '__main__':
    run_test()
