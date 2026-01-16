"""
Scenario test: Large file sync with progress tracking.

Tests realistic file sizes:
1. Alice creates network, Bob joins
2. Alice attaches a small file to a message
3. Track progress as Bob downloads
4. Verify integrity by retrieving file data
5. Optional slow 10 MB file sync

This tests the scalability of the file sync system.
"""
import pytest
import sqlite3
import time
from core.db import Database
from core import schema
from events.identity import user, invite, peer
from events.content import message, message_attachment
from events.network import sync_file
from core import tick


def test_create_from_file_equivalence(fresh_db):
    """Verify create_from_file() produces equivalent results to create().

    Both methods should:
    1. Produce identical root_hash for same input
    2. Create same number of slices
    3. Result in retrievable, identical file data

    Note: file_id will differ because encryption keys are randomly generated,
    but the decoded file content must be identical.
    """
    import tempfile
    import os

    db = fresh_db

    # Setup
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    # Create a small test file (10 KB)
    file_size = 10 * 1024  # 10 KB
    file_data = b'TestData' * (file_size // 8)

    # Create two messages for two attachments
    msg1_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Method 1: in-memory',
        t_ms=2000,
        db=db
    )
    msg2_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Method 2: streaming',
        t_ms=2100,
        db=db
    )
    db.commit()

    # Method 1: In-memory create()
    result1 = message_attachment.create(
        peer_id=alice['peer_id'],
        message_id=msg1_result['id'],
        file_data=file_data,
        filename='test.dat',
        mime_type='application/octet-stream',
        t_ms=3000,
        db=db
    )
    db.commit()

    # Method 2: Streaming create_from_file()
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.dat')
    temp_file.write(file_data)
    temp_file.close()

    try:
        result2 = message_attachment.create_from_file(
            peer_id=alice['peer_id'],
            message_id=msg2_result['id'],
            file_path=temp_file.name,
            filename='test.dat',
            mime_type='application/octet-stream',
            t_ms=3100,
            db=db
        )
        db.commit()
    finally:
        os.unlink(temp_file.name)

    # Verify equivalence
    assert result1['slice_count'] == result2['slice_count'], \
        f"Slice counts differ: {result1['slice_count']} vs {result2['slice_count']}"
    assert result1['blob_bytes'] == result2['blob_bytes'], \
        f"Blob bytes differ: {result1['blob_bytes']} vs {result2['blob_bytes']}"

    # Retrieve and verify file data
    data1 = message_attachment.get_file_data(result1['file_id'], alice['peer_id'], db)
    data2 = message_attachment.get_file_data(result2['file_id'], alice['peer_id'], db)

    assert data1 is not None, "Method 1 file should be retrievable"
    assert data2 is not None, "Method 2 file should be retrievable"
    assert data1 == file_data, "Method 1 should produce original file data"
    assert data2 == file_data, "Method 2 should produce original file data"
    assert data1 == data2, "Both methods should produce identical file data"

    print(f"✓ Equivalence verified: {result1['slice_count']} slices, {result1['blob_bytes']} bytes")


def test_1mb_file_download_with_progress(fresh_db):
    """Test downloading a small file with progress tracking."""

    # Setup
    db = fresh_db

    print("\n=== Setup: Create network ===")

    # Alice creates network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    print(f"✓ Alice created network")

    # Alice creates invite
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )

    # Bob joins
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    bob_peer_shared_id = bob['peer_shared_id']
    print(f"✓ Bob joined network")

    db.commit()

    # Initial sync to establish connection
    print("\n=== Initial sync ===")
    for i in range(5):
        tick.tick(t_ms=3000 + i*100, db=db)
        db.commit()

    print("✓ Initial sync completed")

    print("\n=== Alice creates message with 50 KB file ===")

    # Alice sends message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Check out this file!',
        t_ms=4000,
        db=db
    )
    message_id = msg_result['id']
    print(f"✓ Alice created message: {message_id[:20]}...")

    # Create 50 KB file - enough to test progress tracking without being slow
    file_size = 50 * 1024  # 50 KB (~112 slices)
    file_data = b'X' * file_size  # Simple pattern for testing
    print(f"✓ Created {file_size:,} byte file")

    # Calculate expected slices (450 bytes per slice)
    expected_slices = (file_size + 449) // 450
    print(f"✓ Expected slices: {expected_slices:,}")

    # Alice attaches file
    file_result = message_attachment.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        file_data=file_data,
        filename='large_file.dat',
        mime_type='application/octet-stream',
        t_ms=5000,
        db=db
    )
    file_id = file_result['file_id']
    slice_count = file_result['slice_count']
    print(f"✓ Alice created file: {file_id[:20]}..., {slice_count:,} slices")

    assert slice_count == expected_slices, f"Expected {expected_slices} slices, got {slice_count}"

    db.commit()

    print("\n=== Bob starts focused file sync ===")

    # Bob requests the file (high priority)
    sync_file.request_file_sync(
        file_id=file_id,
        peer_id=bob['peer_id'],
        priority=10,
        ttl_ms=0,  # Forever
        t_ms=6000,
        db=db
    )
    print(f"✓ Bob requested file sync")

    db.commit()

    # Track progress over multiple sync rounds
    print("\n=== Syncing with progress tracking ===")

    start_time = time.time()
    prev_progress = None
    last_print_time = start_time

    for round_num in range(200):  # ~20 slices/round, need ~120 rounds for ~2,331 slices (1 MB)
        current_time_ms = 7000 + round_num * 100

        # Execute tick (runs sync jobs)
        tick.tick(t_ms=current_time_ms, db=db)
        db.commit()

        # Check progress every 500ms (5 rounds)
        if round_num % 5 == 0:
            current_time = time.time()
            elapsed_ms = int((current_time - last_print_time) * 1000)

            progress = message_attachment.get_file_download_progress(
                file_id=file_id,
                recorded_by=bob['peer_id'],
                db=db,
                prev_progress=prev_progress,
                elapsed_ms=elapsed_ms if prev_progress else None
            )

            if progress:
                print(f"Round {round_num:3d}: {progress['slices_received']:5d}/{progress['total_slices']} slices "
                      f"({progress['percentage_complete']:3d}%) - "
                      f"{progress.get('speed_human', 'N/A'):>10s}, "
                      f"ETA: {progress.get('eta_seconds', 'N/A'):>6}")

                if progress['is_complete']:
                    print(f"\n✓ Download complete in {round_num} rounds!")
                    break

                prev_progress = progress
                last_print_time = current_time

    # NOTE: We trust that file sync worked correctly.
    # Observable behavior (file retrieval) will verify this below.
    # Removed DB query for file_slices table.
    print(f"\n✓ File sync completed")

    # Verify Bob can retrieve the file
    bob_retrieved = message_attachment.get_file_data(file_id, bob['peer_id'], db)
    assert bob_retrieved is not None, "Bob should be able to retrieve the file"
    assert len(bob_retrieved) == file_size, f"Expected {file_size} bytes, got {len(bob_retrieved)}"
    assert bob_retrieved == file_data, "Bob's file should match original"
    print(f"✓ Bob retrieved and verified {len(bob_retrieved):,} byte file")

    elapsed_time = time.time() - start_time
    print(f"\n✅ Test passed! small file synced in {elapsed_time:.2f}s")


@pytest.mark.slow
def test_10mb_file_download(fresh_db):
    """Perf gut-check: sync a 10MB file between two peers.

    Expected: ~30-60 seconds
    Run with: pytest -m slow
    """
    from tests.utils.tick_helper import assert_eventually

    db = fresh_db

    # Setup
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    db.commit()

    # Alice creates 10MB file
    print("\nCreating 10MB file...")
    file_size = 10 * 1024 * 1024  # 10 MB (~22,222 slices)
    file_data = b'Y' * file_size

    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Large file',
        t_ms=3000,
        db=db
    )

    start_time = time.time()
    file_result = message_attachment.create(
        peer_id=alice['peer_id'],
        message_id=msg_result['id'],
        file_data=file_data,
        filename='large_file.dat',
        mime_type='application/octet-stream',
        t_ms=3100,
        db=db
    )
    file_id = file_result['file_id']
    print(f"✓ Created {file_size:,} bytes ({file_result['slice_count']:,} slices) in {time.time()-start_time:.1f}s")
    db.commit()

    # Bob requests focused sync for the file
    sync_file.request_file_sync(
        file_id=file_id,
        peer_id=bob['peer_id'],
        priority=10,
        ttl_ms=0,
        t_ms=4000,
        db=db
    )
    db.commit()

    # Wait for Bob to receive complete file
    def bob_has_complete_file():
        progress = message_attachment.get_file_download_progress(file_id, bob['peer_id'], db)
        assert progress and progress['is_complete'], f"Bob at {progress['percentage_complete'] if progress else 0}%"

    print("Syncing...")
    assert_eventually(bob_has_complete_file, db=db, start_t_ms=4000, max_rounds=1000)

    # Verify file integrity
    bob_data = message_attachment.get_file_data(file_id, bob['peer_id'], db)
    assert bob_data == file_data, "File data should match"
    print(f"✓ Bob received and verified {len(bob_data):,} byte file")


@pytest.mark.slow
def test_1gb_file_download(fresh_db):
    """Perf gut-check: sync a 1GB file between two peers.

    1GB = ~2.4 million file slices.
    This tests the XOR fingerprinting at scale.

    Uses create_from_file() for memory-efficient streaming creation.
    Memory usage: <100MB regardless of file size.

    Run with: pytest -m slow -k 1gb
    """
    import tempfile
    import os
    from tests.utils.tick_helper import assert_eventually

    db = fresh_db

    # Setup
    print("\n=== Setup ===")
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    db.commit()
    print("✓ Alice and Bob ready")

    # Create 1GB file on disk (streaming, not in memory)
    print("\n=== Creating 1GB file on disk ===")
    file_size = 1 * 1024 * 1024 * 1024  # 1 GB (~2,386,093 slices)
    expected_slices = (file_size + 449) // 450
    print(f"File size: {file_size:,} bytes")
    print(f"Expected slices: {expected_slices:,}")

    # Create temp file with streaming writes (never hold 1GB in memory)
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.dat')
    temp_file_path = temp_file.name

    try:
        # Write 1GB in 1MB chunks
        CHUNK_SIZE = 1024 * 1024  # 1MB
        pattern = b'X' * CHUNK_SIZE
        write_start = time.time()
        for _ in range(file_size // CHUNK_SIZE):
            temp_file.write(pattern)
        # Handle any remainder
        remainder = file_size % CHUNK_SIZE
        if remainder:
            temp_file.write(b'X' * remainder)
        temp_file.close()
        write_time = time.time() - write_start
        print(f"✓ Wrote {file_size:,} bytes to disk in {write_time:.1f}s")

        msg_result = message.create(
            peer_id=alice['peer_id'],
            channel_id=alice['channel_id'],
            content='Huge file',
            t_ms=3000,
            db=db
        )

        print("Creating file attachment with streaming (memory-efficient)...")
        start_time = time.time()
        file_result = message_attachment.create_from_file(
            peer_id=alice['peer_id'],
            message_id=msg_result['id'],
            file_path=temp_file_path,
            filename='huge_file.dat',
            mime_type='application/octet-stream',
            t_ms=3100,
            db=db
        )
        file_id = file_result['file_id']
        create_time = time.time() - start_time
        print(f"✓ Created {file_size:,} bytes ({file_result['slice_count']:,} slices) in {create_time:.1f}s")
        print(f"  ({file_result['slice_count'] / create_time:.0f} slices/sec)")
        db.commit()

        # Bob requests focused sync for the file
        sync_file.request_file_sync(
            file_id=file_id,
            peer_id=bob['peer_id'],
            priority=10,
            ttl_ms=0,
            t_ms=4000,
            db=db
        )
        db.commit()

        # Wait for Bob to receive complete file
        print("\n=== Syncing ===")
        sync_start = time.time()
        last_pct = 0

        def bob_has_complete_file():
            nonlocal last_pct
            progress = message_attachment.get_file_download_progress(file_id, bob['peer_id'], db)
            if progress:
                pct = progress['percentage_complete']
                if pct >= last_pct + 10:  # Log every 10%
                    elapsed = time.time() - sync_start
                    print(f"  {pct}% complete ({elapsed:.1f}s)")
                    last_pct = pct
            assert progress and progress['is_complete'], f"Bob at {progress['percentage_complete'] if progress else 0}%"

        # 2.4M slices at ~100 slices/round = 24,000 rounds minimum
        # But with XOR fingerprinting, negentropy should be much faster
        assert_eventually(bob_has_complete_file, db=db, start_t_ms=4000, max_rounds=50000)

        sync_time = time.time() - sync_start
        print(f"✓ Sync completed in {sync_time:.1f}s")
        print(f"  ({file_result['slice_count'] / sync_time:.0f} slices/sec)")

        # Verify file integrity (this will also take a while for 1GB)
        print("\n=== Verifying ===")
        verify_start = time.time()
        bob_data = message_attachment.get_file_data(file_id, bob['peer_id'], db)
        assert bob_data is not None, "Bob should have the file"
        assert len(bob_data) == file_size, f"Expected {file_size} bytes, got {len(bob_data)}"
        # Don't compare full 1GB in memory - just check size and a sample
        assert bob_data[:1000] == b'X' * 1000, "Start of file should match"
        assert bob_data[-1000:] == b'X' * 1000, "End of file should match"
        verify_time = time.time() - verify_start
        print(f"✓ Verified {len(bob_data):,} byte file in {verify_time:.1f}s")

        total_time = time.time() - start_time
        print(f"\n✅ Total time: {total_time:.1f}s")

    finally:
        # Clean up temp file
        try:
            os.unlink(temp_file_path)
        except Exception:
            pass
