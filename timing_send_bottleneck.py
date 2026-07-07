#!/usr/bin/env python3
"""Profile the per-blob overhead in _send_event_blobs."""

import time
import os
import sqlite3

import logging
logging.basicConfig(level=logging.CRITICAL)

from core.db import Database, create_unsafe_db, create_safe_db
from core import schema
from core import tick
from core import transport
from core import crypto
from events.identity import user, invite, peer
from events.content import message, message_attachment
from events.network import negentropy, connection_request


def run_test():
    print("=== Per-Blob Overhead Analysis ===\n")

    transport.enable_loopback()
    # Disable pacer to remove that variable
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

    # Initial sync to establish connections
    t_ms = base_t_ms + 2000
    for i in range(100):
        tick.tick(t_ms, db)
        t_ms += 100
    db.commit()

    # Create 500KB file (smaller for faster test)
    print("Creating 500KB file...")
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='File',
        t_ms=t_ms,
        db=db
    )
    t_ms += 100

    file_data = os.urandom(500 * 1024)
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

    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])

    # Get a valid connection_id for Alice
    conn_row = safedb.query_one("""
        SELECT key_id, their_key_id, their_key, peer_shared_id
        FROM connections
        WHERE recorded_by = ? AND their_key IS NOT NULL
        LIMIT 1
    """, (alice['peer_id'],))

    if not conn_row:
        print("No established connection found!")
        return

    connection_id = conn_row['key_id']
    print(f"Using connection: {connection_id[:30]}...")

    # Get some event blobs to send
    event_rows = safedb.query("""
        SELECT event_id FROM shareable_events
        WHERE can_share_peer_id = ?
        LIMIT 1000
    """, (alice['peer_id'],))
    event_ids = [r['event_id'] for r in event_rows]
    print(f"Testing with {len(event_ids)} events\n")

    # Measure individual components

    # 1. Time to fetch blobs
    print("--- Component Timing ---")
    t0 = time.perf_counter()
    blobs = []
    for event_id in event_ids:
        blob = safedb.get_shareable_blob(event_id)
        if blob:
            blobs.append(blob)
    t_fetch = time.perf_counter() - t0
    print(f"Blob fetch ({len(blobs)} blobs): {t_fetch*1000:.1f}ms ({t_fetch/len(blobs)*1000:.3f}ms/blob)")

    # 2. Time for connection lookup (per-blob)
    t0 = time.perf_counter()
    for _ in range(len(blobs)):
        safedb.query_one("""
            SELECT their_key_id, their_key, peer_shared_id
            FROM connections
            WHERE key_id = ? AND recorded_by = ?
        """, (connection_id, alice['peer_id']))
    t_conn_lookup = time.perf_counter() - t0
    print(f"Connection lookup ({len(blobs)}x): {t_conn_lookup*1000:.1f}ms ({t_conn_lookup/len(blobs)*1000:.3f}ms/call)")

    # 3. Time for crypto.wrap (per-blob)
    to_key = {
        'id': crypto.b64decode(conn_row['their_key_id']),
        'key': conn_row['their_key'],
        'type': 'symmetric'
    }
    t0 = time.perf_counter()
    wrapped_blobs = []
    for blob in blobs:
        wrapped = crypto.wrap(blob, to_key, db)
        wrapped_blobs.append(wrapped)
    t_wrap = time.perf_counter() - t0
    print(f"crypto.wrap ({len(blobs)}x): {t_wrap*1000:.1f}ms ({t_wrap/len(blobs)*1000:.3f}ms/blob)")

    # 4. Time for transport.send (per-blob)
    from_addr = ('127.0.0.1', 5000)
    to_addr = ('127.0.0.1', 5001)
    t0 = time.perf_counter()
    for wrapped in wrapped_blobs:
        transport.send(wrapped, from_addr, to_addr)
    t_send = time.perf_counter() - t0
    print(f"transport.send ({len(blobs)}x): {t_send*1000:.1f}ms ({t_send/len(blobs)*1000:.3f}ms/call)")

    # 5. Total per-blob overhead
    total_overhead = t_conn_lookup + t_wrap + t_send
    print(f"\nTotal per-blob overhead: {total_overhead*1000:.1f}ms for {len(blobs)} blobs")
    print(f"Per-blob: {total_overhead/len(blobs)*1000:.3f}ms")

    # 6. Measure batched connection lookup (1 call instead of N)
    print("\n--- Batched vs Per-blob ---")
    t0 = time.perf_counter()
    conn_info = safedb.query_one("""
        SELECT their_key_id, their_key, peer_shared_id
        FROM connections
        WHERE key_id = ? AND recorded_by = ?
    """, (connection_id, alice['peer_id']))
    t_batch_lookup = time.perf_counter() - t0
    print(f"Batched connection lookup (1x): {t_batch_lookup*1000:.3f}ms")
    print(f"Savings: {(t_conn_lookup - t_batch_lookup)*1000:.1f}ms ({(t_conn_lookup/t_batch_lookup):.0f}x faster)")

    # Calculate potential throughput
    total_bytes = sum(len(b) for b in blobs)
    current_time = t_fetch + total_overhead
    batched_time = t_fetch + t_batch_lookup + t_wrap + t_send

    print(f"\n--- Throughput Projection ---")
    print(f"Total data: {total_bytes/1024:.1f} KB")
    print(f"Current time: {current_time*1000:.1f}ms = {total_bytes/1024/1024/current_time:.1f} MB/s")
    print(f"With batched lookup: {batched_time*1000:.1f}ms = {total_bytes/1024/1024/batched_time:.1f} MB/s")
    print(f"Crypto alone: {t_wrap*1000:.1f}ms = {total_bytes/1024/1024/t_wrap:.1f} MB/s (crypto ceiling)")


if __name__ == '__main__':
    run_test()
