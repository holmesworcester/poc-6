"""
Scenario test: 10MB file sync via negentropy with adaptive splitting.

This is the real end-to-end test for large file sync performance.
A 10MB file creates ~23,000 file_slice events at the same timestamp.

Without adaptive splitting: ~23,000 ranges (prefix drilling to prefix_10)
With adaptive splitting: ~8 ranges (log2(23000/100) binary splits)

This test verifies:
1. Alice can create a 10MB file
2. Negentropy sync transfers all slices to Bob
3. Bob can reassemble and verify the file
4. Performance is reasonable (not exponential in file size)
"""
import sys
import time

def log(msg):
    """Write to stderr for visibility during tests."""
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()
import pytest
from core.db import Database
from core import schema
from events.identity import user, invite, peer
from events.content import channel, message, message_attachment
from core import tick


def test_10mb_file_sync(fresh_db):
    """End-to-end test: sync a 10MB file from Alice to Bob."""

    db = fresh_db

    log("\n" + "="*60)
    log("10MB FILE SYNC TEST - Adaptive Splitting")
    log("="*60)

    # === Setup ===
    log("\n--- Setup: Create networks ---")

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    log(f"Alice peer_id: {alice['peer_id'][:16]}...")

    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )

    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    log(f"Bob peer_id: {bob['peer_id'][:16]}...")

    db.commit()

    # Initial sync for bootstrap
    log("\n--- Initial sync (bootstrap) ---")
    for i in range(10):
        tick.tick(t_ms=2100 + i*100, db=db)

    # === Create 10MB file ===
    log("\n--- Creating 10MB file ---")

    # 10MB = 10,485,760 bytes
    # Using a repeating pattern for easy verification
    file_size = 10 * 1024 * 1024  # 10MB
    pattern = b'ABCDEFGHIJ' * 100  # 1000 byte pattern
    file_data = (pattern * (file_size // len(pattern) + 1))[:file_size]
    assert len(file_data) == file_size, f"File should be {file_size} bytes"

    # Create message first
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='10MB file attachment',
        t_ms=3000,
        db=db
    )
    message_id = msg_result['id']

    # Create file attachment
    start_create = time.time()
    file_result = message_attachment.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        file_data=file_data,
        filename='large_test.bin',
        mime_type='application/octet-stream',
        t_ms=5000,
        db=db
    )
    create_time = time.time() - start_create

    file_id = file_result['file_id']
    slice_count = file_result['slice_count']

    log(f"File ID: {file_id[:16]}...")
    log(f"File size: {file_size:,} bytes ({file_size / 1024 / 1024:.1f} MB)")
    log(f"Slice count: {slice_count:,}")
    log(f"Creation time: {create_time:.2f}s")

    # Verify Alice has all slices
    alice_slices = db.query_one(
        "SELECT COUNT(*) as cnt FROM file_slices WHERE file_id = ? AND recorded_by = ?",
        (file_id, alice['peer_id'])
    )
    assert alice_slices['cnt'] == slice_count, f"Alice should have {slice_count} slices"
    log(f"Alice has {alice_slices['cnt']:,} slices")

    db.commit()

    # === Sync to Bob ===
    log("\n--- Syncing to Bob ---")

    start_sync = time.time()
    max_rounds = 100

    for round_num in range(max_rounds):
        tick.tick(t_ms=6000 + round_num * 100, db=db)

        # Check Bob's progress
        bob_slice_count = db.query_one(
            "SELECT COUNT(*) as cnt FROM file_slices WHERE file_id = ? AND recorded_by = ?",
            (file_id, bob['peer_id'])
        )
        bob_count = bob_slice_count['cnt'] if bob_slice_count else 0

        # Debug: check message flow
        if round_num % 10 == 0 or bob_count == slice_count:
            incoming_log = db.query_one(
                "SELECT COUNT(*) as cnt FROM incoming_event_log"
            )
            bob_events = db.query_one(
                "SELECT COUNT(*) as cnt FROM negentropy_events WHERE recorded_by = ?",
                (bob['peer_id'],)
            )
            neg_sync = db.query_one("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
                    SUM(CASE WHEN status = 'matched' THEN 1 ELSE 0 END) as matched,
                    SUM(CASE WHEN status = 'events_sent' THEN 1 ELSE 0 END) as events_sent
                FROM negentropy_sync_state
                WHERE recorded_by = ?
            """, (bob['peer_id'],))
            pct = (bob_count / slice_count * 100) if slice_count > 0 else 0
            # Check how many are negentropy vs other
            neg_incoming = db.query_one(
                "SELECT COUNT(*) as cnt FROM incoming_event_log WHERE event_type = 'negentropy'"
            )
            other_incoming = db.query_one(
                "SELECT COUNT(*) as cnt FROM incoming_event_log WHERE event_type IS NULL OR event_type != 'negentropy'"
            )
            log(f"  Round {round_num:3d}: slices={bob_count:,}/{slice_count:,} ({pct:.1f}%) "
                f"neg_events={bob_events['cnt']} incoming={incoming_log['cnt']}(neg={neg_incoming['cnt']},other={other_incoming['cnt']}) "
                f"neg_ranges={neg_sync['total']}(sent={neg_sync['events_sent']})")

        if bob_count == slice_count:
            break

    sync_time = time.time() - start_sync
    rounds_used = round_num + 1

    log(f"\nSync completed in {rounds_used} rounds, {sync_time:.2f}s")

    # === Verify Bob received everything ===
    log("\n--- Verification ---")

    # Check slice count
    bob_final = db.query_one(
        "SELECT COUNT(*) as cnt FROM file_slices WHERE file_id = ? AND recorded_by = ?",
        (file_id, bob['peer_id'])
    )
    assert bob_final['cnt'] == slice_count, \
        f"Bob should have {slice_count} slices, got {bob_final['cnt']}"
    log(f"Bob has all {bob_final['cnt']:,} slices")

    # Verify file content
    bob_file = message_attachment.get_file_data(file_id, bob['peer_id'], db)
    assert bob_file is not None, "Bob should be able to retrieve file"
    assert len(bob_file) == file_size, f"File size should be {file_size}, got {len(bob_file)}"
    assert bob_file == file_data, "File content should match original"
    log(f"File content verified: {len(bob_file):,} bytes match")

    # === Performance summary ===
    log("\n" + "="*60)
    log("PERFORMANCE SUMMARY")
    log("="*60)
    log(f"File size:      {file_size:,} bytes ({file_size / 1024 / 1024:.1f} MB)")
    log(f"Slices:         {slice_count:,}")
    log(f"Creation time:  {create_time:.2f}s")
    log(f"Sync rounds:    {rounds_used}")
    log(f"Sync time:      {sync_time:.2f}s")
    log(f"Throughput:     {file_size / sync_time / 1024 / 1024:.1f} MB/s")
    log("="*60)

    # Sanity check: sync shouldn't take too many rounds
    # With adaptive splitting, expect ~20-30 rounds for 23k slices
    # (negentropy discovery + blob transfer)
    assert rounds_used < 80, f"Sync took too many rounds: {rounds_used}"


def test_10mb_file_sync_negentropy_stats(fresh_db):
    """Test 10MB sync with negentropy statistics tracking."""

    db = fresh_db

    log("\n" + "="*60)
    log("10MB FILE SYNC - NEGENTROPY STATISTICS")
    log("="*60)

    # Setup
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    db.commit()

    # Bootstrap sync
    for i in range(10):
        tick.tick(t_ms=2100 + i*100, db=db)

    # Create 10MB file
    file_size = 10 * 1024 * 1024
    file_data = bytes(range(256)) * (file_size // 256 + 1)
    file_data = file_data[:file_size]

    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Stats test',
        t_ms=3000,
        db=db
    )

    file_result = message_attachment.create(
        peer_id=alice['peer_id'],
        message_id=msg_result['id'],
        file_data=file_data,
        filename='stats_test.bin',
        mime_type='application/octet-stream',
        t_ms=5000,
        db=db
    )

    file_id = file_result['file_id']
    slice_count = file_result['slice_count']
    log(f"\nFile: {slice_count:,} slices")

    db.commit()

    # Track negentropy ranges during sync
    for round_num in range(60):
        tick.tick(t_ms=6000 + round_num * 100, db=db)

        # Count negentropy sync state entries
        neg_stats = db.query_one("""
            SELECT
                COUNT(*) as total_ranges,
                SUM(CASE WHEN status = 'matched' THEN 1 ELSE 0 END) as matched,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
                SUM(CASE WHEN level = 255 THEN 1 ELSE 0 END) as adaptive_ranges
            FROM negentropy_sync_state
            WHERE recorded_by = ?
        """, (bob['peer_id'],))

        bob_slices = db.query_one(
            "SELECT COUNT(*) as cnt FROM file_slices WHERE file_id = ? AND recorded_by = ?",
            (file_id, bob['peer_id'])
        )

        if round_num % 5 == 0:
            log(f"Round {round_num:2d}: slices={bob_slices['cnt']:,}/{slice_count:,} "
                  f"ranges={neg_stats['total_ranges'] or 0} "
                  f"(adaptive={neg_stats['adaptive_ranges'] or 0})")

        if bob_slices['cnt'] == slice_count:
            log(f"\nSync complete at round {round_num}")
            log(f"Total negentropy ranges created: {neg_stats['total_ranges']}")
            log(f"Adaptive ranges: {neg_stats['adaptive_ranges']}")
            break

    # Final verification
    bob_file = message_attachment.get_file_data(file_id, bob['peer_id'], db)
    assert bob_file == file_data, "File content mismatch"
    log("\nFile verified successfully")
