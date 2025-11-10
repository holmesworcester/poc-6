"""
Scenario test: Large file sync with progress tracking.

Tests realistic file sizes:
1. Alice creates network, Bob joins
2. Alice attaches 5 MB file to message
3. Track progress as Bob downloads
4. Verify integrity with root_hash
5. Test 50 MB file as well

This tests the scalability of the file sync system.
"""
import sqlite3
import time
from db import Database
import schema
from events.identity import user, invite
from events.content import message, message_attachment
from events.transit import sync_file
import tick


def test_5mb_file_download_with_progress():
    """Test downloading a realistic 5 MB file with progress tracking."""

    # Setup
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

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
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    print(f"✓ Bob joined network")

    db.commit()

    # Initial sync to establish connection
    print("\n=== Initial sync ===")
    for i in range(5):
        tick.tick(t_ms=3000 + i*100, db=db)
        db.commit()

    print("✓ Initial sync completed")

    print("\n=== Alice creates message with 5 MB file ===")

    # Alice sends message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Check out this 5 MB file!',
        t_ms=4000,
        db=db
    )
    message_id = msg_result['id']
    print(f"✓ Alice created message: {message_id[:20]}...")

    # Create 5 MB file (5,000,000 bytes)
    file_size = 5 * 1024 * 1024  # 5 MB
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

    for round_num in range(100):  # Enough rounds to complete a large file
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

    # Verify Bob has all slices
    bob_slices = db.query_all(
        "SELECT slice_number FROM file_slices WHERE file_id = ? AND recorded_by = ? ORDER BY slice_number",
        (file_id, bob['peer_id'])
    )
    print(f"\n✓ Bob has {len(bob_slices):,} slices")
    assert len(bob_slices) == expected_slices, f"Expected {expected_slices} slices, got {len(bob_slices)}"

    # Verify Bob can retrieve the file
    from events.content import file_slice
    bob_retrieved = file_slice.get_file(file_id, bob['peer_id'], db)
    assert bob_retrieved is not None, "Bob should be able to retrieve the file"
    assert len(bob_retrieved) == file_size, f"Expected {file_size} bytes, got {len(bob_retrieved)}"
    assert bob_retrieved == file_data, "Bob's file should match original"
    print(f"✓ Bob retrieved and verified {len(bob_retrieved):,} byte file")

    elapsed_time = time.time() - start_time
    print(f"\n✅ Test passed! 5 MB file synced in {elapsed_time:.2f}s")


def test_50mb_file_download():
    """Test downloading a very large 50 MB file."""

    # Setup
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    print("\n=== Setup: Create network ===")

    # Alice creates network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Alice creates invite
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )

    # Bob joins
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    print(f"✓ Bob joined network")

    db.commit()

    # Initial sync
    print("\n=== Initial sync ===")
    for i in range(5):
        tick.tick(t_ms=3000 + i*100, db=db)
        db.commit()

    print("\n=== Alice creates message with 50 MB file ===")

    # Alice sends message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Check out this 50 MB file!',
        t_ms=4000,
        db=db
    )
    message_id = msg_result['id']

    # Create 50 MB file
    file_size = 50 * 1024 * 1024  # 50 MB
    file_data = b'Y' * file_size
    print(f"✓ Created {file_size:,} byte file")

    expected_slices = (file_size + 449) // 450
    print(f"✓ Expected slices: {expected_slices:,}")

    # Alice attaches file
    start_time = time.time()
    file_result = message_attachment.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        file_data=file_data,
        filename='very_large_file.dat',
        mime_type='application/octet-stream',
        t_ms=5000,
        db=db
    )
    file_id = file_result['file_id']
    slice_count = file_result['slice_count']

    creation_time = time.time() - start_time
    print(f"✓ Alice created file in {creation_time:.2f}s: {slice_count:,} slices")

    db.commit()

    print("\n=== Bob starts focused file sync ===")

    # Bob requests the file
    sync_file.request_file_sync(
        file_id=file_id,
        peer_id=bob['peer_id'],
        priority=10,
        ttl_ms=0,
        t_ms=6000,
        db=db
    )

    db.commit()

    # Sync with progress tracking
    print("\n=== Syncing large file ===")
    print("(This may take a while...)")

    start_time = time.time()
    prev_progress = None
    last_print_time = start_time

    for round_num in range(1000):  # Many rounds for 50 MB
        current_time_ms = 7000 + round_num * 100

        tick.tick(t_ms=current_time_ms, db=db)
        db.commit()

        # Print progress every 20 rounds
        if round_num % 20 == 0:
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
                print(f"Round {round_num:4d}: {progress['percentage_complete']:3d}% - "
                      f"{progress.get('speed_human', 'N/A'):>10s}")

                if progress['is_complete']:
                    print(f"\n✓ Download complete in {round_num} rounds!")
                    break

                prev_progress = progress
                last_print_time = current_time

    # Quick verification (don't compare full data for 50 MB)
    bob_slices = db.query_all(
        "SELECT COUNT(*) as count FROM file_slices WHERE file_id = ? AND recorded_by = ?",
        (file_id, bob['peer_id'])
    )
    slice_count_received = bob_slices[0]['count']
    print(f"\n✓ Bob has {slice_count_received:,} slices")
    assert slice_count_received == expected_slices

    elapsed_time = time.time() - start_time
    print(f"\n✅ Test passed! 50 MB file synced in {elapsed_time:.2f}s")


if __name__ == '__main__':
    print("=" * 80)
    print("TEST 1: 5 MB file download with progress tracking")
    print("=" * 80)
    test_5mb_file_download_with_progress()

    print("\n" + "=" * 80)
    print("TEST 2: 50 MB file download")
    print("=" * 80)
    test_50mb_file_download()

    print("\n" + "=" * 80)
    print("ALL TESTS PASSED!")
    print("=" * 80)
