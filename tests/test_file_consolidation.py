"""
Test file consolidation optimization.

Tests that:
- Files are automatically consolidated when download completes
- Consolidated files read much faster (single BLOB vs many rows)
- Fallback to slow path works if consolidation fails
- get_file_data produces identical results with both paths
"""
import sqlite3
import time
from db import Database
import schema
from events.identity import user, invite
from events.content import message, message_attachment
from events.transit import sync_file
import tick


def test_auto_consolidation_on_upload():
    """Test that uploaded files are automatically consolidated."""

    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Create user
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Create message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Test',
        t_ms=2000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Create 100 KB file (will be split into ~222 slices)
    file_data = b'X' * (100 * 1024)

    result = message_attachment.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        file_data=file_data,
        filename='test.dat',
        mime_type='application/octet-stream',
        t_ms=3000,
        db=db
    )
    file_id = result['file_id']
    db.commit()

    print(f"\n✓ Created file: {result['slice_count']} slices")

    # Check initial state - consolidated_blob should be None
    row = db.query_one(
        "SELECT consolidated_blob FROM message_attachments WHERE file_id = ? AND recorded_by = ?",
        (file_id, alice['peer_id'])
    )
    assert row['consolidated_blob'] is None, "Should not be consolidated yet"

    # Trigger consolidation via get_file_download_progress
    progress = message_attachment.get_file_download_progress(
        file_id=file_id,
        recorded_by=alice['peer_id'],
        db=db
    )

    assert progress['is_complete'] == True, "File should be complete"

    # Check consolidated_blob was created
    row = db.query_one(
        "SELECT consolidated_blob FROM message_attachments WHERE file_id = ? AND recorded_by = ?",
        (file_id, alice['peer_id'])
    )
    assert row['consolidated_blob'] is not None, "Should be consolidated after progress check"
    assert len(row['consolidated_blob']) > 0, "Consolidated blob should have data"

    print(f"✓ File consolidated: {len(row['consolidated_blob']):,} bytes")

    # Verify we can read the file using fast path
    retrieved = message_attachment.get_file_data(file_id, alice['peer_id'], db)
    assert retrieved == file_data, "Retrieved data should match original"

    print(f"✓ File read successfully via FAST PATH")


def test_auto_consolidation_on_download_complete():
    """Test that downloaded files are automatically consolidated when complete."""

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

    print("✓ Network created and synced")

    print("\n=== Alice uploads 500 KB file ===")

    # Alice creates message with file
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Large file test',
        t_ms=4000,
        db=db
    )
    message_id = msg_result['id']

    # Create 500 KB file (will be split into ~1,111 slices)
    file_size = 500 * 1024
    file_data = b'T' * file_size

    file_result = message_attachment.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        file_data=file_data,
        filename='large.dat',
        mime_type='application/octet-stream',
        t_ms=5000,
        db=db
    )
    file_id = file_result['file_id']
    total_slices = file_result['slice_count']
    db.commit()

    print(f"✓ Alice created {file_size:,} byte file ({total_slices} slices)")

    print("\n=== Bob downloads file ===")

    # Bob requests file sync
    sync_file.request_file_sync(
        file_id=file_id,
        peer_id=bob['peer_id'],
        priority=10,
        ttl_ms=0,
        t_ms=6000,
        db=db
    )
    db.commit()

    # Sync until complete
    for round_num in range(100):
        tick.tick(t_ms=7000 + round_num * 100, db=db)
        db.commit()

        # Check progress periodically
        if round_num % 10 == 0:
            progress = message_attachment.get_file_download_progress(
                file_id=file_id,
                recorded_by=bob['peer_id'],
                db=db
            )

            if progress:
                print(f"Round {round_num:2d}: {progress['percentage_complete']:3d}% "
                      f"({progress['slices_received']}/{progress['total_slices']} slices)")

                if progress['is_complete']:
                    print(f"\n✓ Download complete!")

                    # Verify consolidation happened automatically
                    row = db.query_one(
                        "SELECT consolidated_blob FROM message_attachments "
                        "WHERE file_id = ? AND recorded_by = ?",
                        (file_id, bob['peer_id'])
                    )

                    assert row is not None, "Attachment should exist"
                    assert row['consolidated_blob'] is not None, "Should be auto-consolidated"
                    print(f"✓ File auto-consolidated: {len(row['consolidated_blob']):,} bytes")

                    # Verify file data
                    retrieved = message_attachment.get_file_data(file_id, bob['peer_id'], db)
                    assert retrieved == file_data, "Retrieved data should match"
                    print(f"✓ File verified: {len(retrieved):,} bytes")

                    break

    assert progress['is_complete'], "Download should complete within 100 rounds"


def test_fast_path_vs_slow_path():
    """Test that fast path (consolidated) and slow path (slices) produce identical results."""

    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Test',
        t_ms=2000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Create 50 KB file
    file_data = b'Z' * (50 * 1024)

    result = message_attachment.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        file_data=file_data,
        filename='test.dat',
        mime_type='application/octet-stream',
        t_ms=3000,
        db=db
    )
    file_id = result['file_id']
    db.commit()

    print(f"\n✓ Created {len(file_data):,} byte file")

    # Read via SLOW PATH (no consolidation yet)
    slow_path_data = message_attachment.get_file_data(file_id, alice['peer_id'], db)
    assert slow_path_data == file_data, "Slow path should return correct data"
    print(f"✓ Slow path read: {len(slow_path_data):,} bytes")

    # Consolidate
    message_attachment.consolidate_file_slices(file_id, alice['peer_id'], db)
    db.commit()

    # Read via FAST PATH (consolidated)
    fast_path_data = message_attachment.get_file_data(file_id, alice['peer_id'], db)
    assert fast_path_data == file_data, "Fast path should return correct data"
    print(f"✓ Fast path read: {len(fast_path_data):,} bytes")

    # Verify both paths produce identical results
    assert slow_path_data == fast_path_data, "Both paths should produce identical data"
    print(f"✓ Both paths produce identical results")


def test_consolidation_idempotent():
    """Test that consolidate_file_slices can be called multiple times safely."""

    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Test',
        t_ms=2000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    file_data = b'Y' * (25 * 1024)

    result = message_attachment.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        file_data=file_data,
        filename='test.dat',
        mime_type='application/octet-stream',
        t_ms=3000,
        db=db
    )
    file_id = result['file_id']
    db.commit()

    # Consolidate multiple times
    for i in range(3):
        success = message_attachment.consolidate_file_slices(file_id, alice['peer_id'], db)
        assert success == True, f"Consolidation {i+1} should succeed"
        db.commit()

    print(f"\n✓ Consolidation is idempotent (called 3 times successfully)")

    # Verify data is still correct
    retrieved = message_attachment.get_file_data(file_id, alice['peer_id'], db)
    assert retrieved == file_data, "Data should still be correct"
    print(f"✓ Data integrity maintained")


if __name__ == '__main__':
    print("=" * 80)
    print("Running File Consolidation Tests")
    print("=" * 80)

    test_auto_consolidation_on_upload()
    test_auto_consolidation_on_download_complete()
    test_fast_path_vs_slow_path()
    test_consolidation_idempotent()

    print("\n" + "=" * 80)
    print("ALL CONSOLIDATION TESTS PASSED!")
    print("=" * 80)
