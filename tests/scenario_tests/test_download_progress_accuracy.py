"""
Test download progress accuracy.

Verifies that download progress tracking reports accurate:
- Slice counts
- Byte counts (actual ciphertext bytes received)
- Speed calculations
- ETA estimates
- Percentage completion
"""
import sqlite3
import time
from db import Database
import schema
from events.identity import user, invite
from events.content import message, message_attachment
from events.transit import sync_file
import tick


def test_progress_bytes_accuracy():
    """Test that bytes_received matches actual ciphertext bytes."""

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

    print("✓ Initial sync completed")

    print("\n=== Alice creates message with 100 KB file ===")

    # Alice sends message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Test file',
        t_ms=4000,
        db=db
    )
    message_id = msg_result['id']

    # Create 100 KB file
    file_size = 100 * 1024  # 100 KB
    file_data = b'T' * file_size
    print(f"✓ Created {file_size:,} byte file")

    # Alice attaches file
    file_result = message_attachment.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        file_data=file_data,
        filename='test.dat',
        mime_type='application/octet-stream',
        t_ms=5000,
        db=db
    )
    file_id = file_result['file_id']
    total_slices = file_result['slice_count']
    print(f"✓ Alice created file: {total_slices:,} slices")

    db.commit()

    print("\n=== Bob starts file sync ===")

    # Bob requests the file
    sync_file.request_file_sync(
        file_id=file_id,
        peer_id=bob['peer_id'],
        priority=10,
        ttl_ms=0,
        t_ms=6000,
        db=db
    )
    print(f"✓ Bob requested file sync")

    db.commit()

    print("\n=== Test progress accuracy ===")

    # Sync for a few rounds
    for round_num in range(20):
        current_time_ms = 7000 + round_num * 100
        tick.tick(t_ms=current_time_ms, db=db)
        db.commit()

        # Check progress every few rounds
        if round_num % 5 == 0:
            progress = message_attachment.get_file_download_progress(
                file_id=file_id,
                recorded_by=bob['peer_id'],
                db=db
            )

            if progress:
                # Verify bytes_received matches actual ciphertext bytes in DB
                actual_bytes = db.query_one(
                    "SELECT SUM(LENGTH(ciphertext)) as total FROM file_slices "
                    "WHERE file_id = ? AND recorded_by = ?",
                    (file_id, bob['peer_id'])
                )
                actual_bytes_received = actual_bytes['total'] if actual_bytes and actual_bytes['total'] else 0

                print(f"Round {round_num:2d}: {progress['slices_received']:3d}/{progress['total_slices']} slices, "
                      f"{progress['bytes_received']:6d} bytes (actual: {actual_bytes_received:6d}), "
                      f"{progress['percentage_complete']:3d}%")

                # Verify bytes_received matches database
                assert progress['bytes_received'] == actual_bytes_received, \
                    f"Progress reports {progress['bytes_received']} bytes but DB has {actual_bytes_received}"

                # Verify percentage calculation
                expected_pct = int((progress['slices_received'] / progress['total_slices']) * 100)
                assert progress['percentage_complete'] == expected_pct, \
                    f"Expected {expected_pct}% but got {progress['percentage_complete']}%"

                if progress['is_complete']:
                    print(f"\n✓ Download complete!")
                    break

    print("\n✅ Progress bytes accuracy test passed!")


def test_speed_and_eta_accuracy():
    """Test that speed and ETA calculations are accurate."""

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

    db.commit()

    # Initial sync
    for i in range(5):
        tick.tick(t_ms=3000 + i*100, db=db)
        db.commit()

    print("\n=== Alice creates message with 1 MB file ===")

    # Alice sends message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Large test file',
        t_ms=4000,
        db=db
    )
    message_id = msg_result['id']

    # Create 1 MB file
    file_size = 1 * 1024 * 1024  # 1 MB
    file_data = b'S' * file_size
    print(f"✓ Created {file_size:,} byte file")

    # Alice attaches file
    file_result = message_attachment.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        file_data=file_data,
        filename='large_test.dat',
        mime_type='application/octet-stream',
        t_ms=5000,
        db=db
    )
    file_id = file_result['file_id']
    total_slices = file_result['slice_count']
    print(f"✓ Alice created file: {total_slices:,} slices")

    db.commit()

    print("\n=== Bob starts file sync ===")

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

    print("\n=== Test speed and ETA calculations ===")

    prev_progress = None
    start_time = time.time()
    last_check_time = start_time

    for round_num in range(100):
        current_time_ms = 7000 + round_num * 100
        tick.tick(t_ms=current_time_ms, db=db)
        db.commit()

        # Check progress every 10 rounds (simulating UI updates every second)
        if round_num % 10 == 0 and round_num > 0:
            current_time = time.time()
            elapsed_ms = int((current_time - last_check_time) * 1000)

            progress = message_attachment.get_file_download_progress(
                file_id=file_id,
                recorded_by=bob['peer_id'],
                db=db,
                prev_progress=prev_progress,
                elapsed_ms=elapsed_ms if prev_progress else None
            )

            if progress:
                # Verify speed calculation makes sense
                if prev_progress and elapsed_ms > 0:
                    bytes_delta = progress['bytes_received'] - prev_progress['bytes_received']
                    expected_speed = int(bytes_delta / (elapsed_ms / 1000.0))

                    # Allow some rounding tolerance
                    speed_diff = abs(progress['speed_bytes_per_sec'] - expected_speed)
                    assert speed_diff <= 1, \
                        f"Speed mismatch: got {progress['speed_bytes_per_sec']}, expected {expected_speed}"

                    print(f"Round {round_num:3d}: {progress['percentage_complete']:3d}% - "
                          f"{progress['speed_human']:>12s}, "
                          f"ETA: {progress.get('eta_seconds', 'N/A'):>6}s")

                    # Verify ETA makes sense (should be positive and reasonable when not complete)
                    if 'eta_seconds' in progress and not progress['is_complete']:
                        # ETA can be 0 if download is nearly complete or speed is very high
                        assert progress['eta_seconds'] >= 0, "ETA should be non-negative"
                        assert progress['eta_seconds'] < 10000, "ETA should be reasonable"

                if progress['is_complete']:
                    total_time = time.time() - start_time
                    print(f"\n✓ Download complete in {total_time:.2f}s!")
                    break

                prev_progress = progress
                last_check_time = current_time

    print("\n✅ Speed and ETA accuracy test passed!")


def test_bytes_increase_monotonically():
    """Test that bytes_received increases monotonically during download."""

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

    db.commit()

    # Initial sync
    for i in range(5):
        tick.tick(t_ms=3000 + i*100, db=db)
        db.commit()

    print("\n=== Alice creates message with 500 KB file ===")

    # Alice creates larger file to ensure we can observe incremental progress
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Large test',
        t_ms=4000,
        db=db
    )

    file_size = 500 * 1024  # 500 KB
    file_result = message_attachment.create(
        peer_id=alice['peer_id'],
        message_id=msg_result['id'],
        file_data=b'M' * file_size,
        filename='monotonic_test.dat',
        mime_type='application/octet-stream',
        t_ms=5000,
        db=db
    )
    file_id = file_result['file_id']
    total_slices = file_result['slice_count']
    print(f"✓ Created {file_size:,} byte file with {total_slices:,} slices")

    db.commit()

    print("\n=== Bob starts file sync ===")

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

    print("\n=== Verify bytes increase monotonically ===")

    prev_bytes = 0
    prev_slices = 0

    for round_num in range(50):
        current_time_ms = 7000 + round_num * 100
        tick.tick(t_ms=current_time_ms, db=db)
        db.commit()

        progress = message_attachment.get_file_download_progress(
            file_id=file_id,
            recorded_by=bob['peer_id'],
            db=db
        )

        if progress:
            # Verify bytes never decrease
            assert progress['bytes_received'] >= prev_bytes, \
                f"Bytes decreased from {prev_bytes} to {progress['bytes_received']}"

            # Verify slices never decrease
            assert progress['slices_received'] >= prev_slices, \
                f"Slices decreased from {prev_slices} to {progress['slices_received']}"

            # If slices increased, bytes should also increase
            if progress['slices_received'] > prev_slices:
                assert progress['bytes_received'] > prev_bytes, \
                    f"Slices increased but bytes didn't: {prev_bytes} -> {progress['bytes_received']}"

                print(f"Round {round_num:2d}: {progress['slices_received']:4d}/{progress['total_slices']} slices, "
                      f"{progress['bytes_received']:7d} bytes (+{progress['bytes_received'] - prev_bytes:5d})")

            prev_bytes = progress['bytes_received']
            prev_slices = progress['slices_received']

            if progress['is_complete']:
                print(f"\n✓ Download complete!")
                break

    print("\n✅ Bytes increase monotonically test passed!")


if __name__ == '__main__':
    print("=" * 80)
    print("TEST 1: Progress bytes accuracy")
    print("=" * 80)
    test_progress_bytes_accuracy()

    print("\n" + "=" * 80)
    print("TEST 2: Speed and ETA accuracy")
    print("=" * 80)
    test_speed_and_eta_accuracy()

    print("\n" + "=" * 80)
    print("TEST 3: Bytes increase monotonically")
    print("=" * 80)
    test_bytes_increase_monotonically()

    print("\n" + "=" * 80)
    print("ALL TESTS PASSED!")
    print("=" * 80)
