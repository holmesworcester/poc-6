"""
Performance tests for large file sync.

Tests that file sync completes for various file sizes:
- 1MB file (~2330 slices at 450 bytes/slice)
- 10MB file (~23302 slices)
- 100MB file (~233017 slices)
- 200MB file (~466034 slices)

Run with: PYTHONPATH=. pytest tests/test_sync_perf_files.py -v -s -m slow
"""
import pytest
import time
from events.identity import user, invite, peer
from events.content import message, message_attachment
from tests.utils import tick_helper
from tests.utils.tick_helper import TestClock, assert_eventually


def _create_file_data(size_bytes: int) -> bytes:
    """Create test file data of specified size."""
    # Create repeating pattern that's compressible but still realistic
    pattern = b'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\n' * 100
    repetitions = (size_bytes // len(pattern)) + 1
    return (pattern * repetitions)[:size_bytes]


def _run_file_sync_test(fresh_db, file_size_bytes: int, max_rounds: int = 1000):
    """Run a file sync test and report performance metrics."""
    db = fresh_db
    clock = TestClock()
    size_name = f"{file_size_bytes // (1024*1024)}MB" if file_size_bytes >= 1024*1024 else f"{file_size_bytes // 1024}KB"

    print(f"\n{'='*60}")
    print(f"File Sync Performance Test: {size_name} ({file_size_bytes:,} bytes)")
    print(f"{'='*60}")

    # Setup: Alice creates network, Bob joins
    print("\n[Setup] Creating network...")
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)

    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.now(), db=db)
    db.commit()

    # Initial sync
    print("[Setup] Running initial sync...")
    tick_helper.initial_sync(db, start_t_ms=None)

    # Alice creates message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content=f'Here is a {size_name} file',
        t_ms=clock.tick(),
        db=db
    )
    message_id = msg_result['id']

    # Alice creates the file
    print(f"[Create] Generating {size_name} file data...")
    start_create = time.time()
    file_data = _create_file_data(file_size_bytes)
    assert len(file_data) == file_size_bytes

    print(f"[Create] Creating file attachment...")
    file_result = message_attachment.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        file_data=file_data,
        filename=f'test_{size_name}.bin',
        mime_type='application/octet-stream',
        t_ms=clock.tick(),
        db=db
    )
    file_id = file_result['file_id']
    slice_count = file_result['slice_count']
    create_time = time.time() - start_create
    print(f"[Create] Created file: {slice_count:,} slices in {create_time:.2f}s")
    db.commit()

    # Sync until complete using assert_eventually
    print(f"\n[Sync] Starting sync...")
    start_sync = time.time()

    def file_sync_complete():
        progress = message_attachment.get_file_download_progress(file_id, bob['peer_id'], db)
        assert progress and progress['is_complete'], \
            f"Sync incomplete: {progress['slices_received'] if progress else 0}/{slice_count} slices"

    assert_eventually(file_sync_complete, db=db, start_t_ms=None, max_rounds=max_rounds)
    sync_time = time.time() - start_sync

    # Verify data integrity
    print(f"\n[Verify] Checking data integrity...")
    bob_data = message_attachment.get_file_data(file_id, bob['peer_id'], db)
    assert bob_data == file_data, "Data integrity check failed"
    print(f"  Data integrity verified ({slice_count:,} slices)")

    # Report metrics
    print(f"\n{'='*60}")
    print(f"Results: {size_name} File Sync")
    print(f"{'='*60}")
    print(f"  File size:      {file_size_bytes:,} bytes ({slice_count:,} slices)")
    print(f"  Sync time:      {sync_time:.2f}s")
    print(f"  Create time:    {create_time:.2f}s")
    print(f"{'='*60}")

    return {
        'file_size': file_size_bytes,
        'slice_count': slice_count,
        'sync_time': sync_time,
        'create_time': create_time,
    }


@pytest.mark.slow
def test_sync_perf_1mb_file(fresh_db):
    """Performance test: sync a 1MB file (~2,330 slices)."""
    _run_file_sync_test(fresh_db, file_size_bytes=1 * 1024 * 1024)


@pytest.mark.slow
def test_sync_perf_10mb_file(fresh_db):
    """Performance test: sync a 10MB file (~23,302 slices)."""
    _run_file_sync_test(fresh_db, file_size_bytes=10 * 1024 * 1024)


@pytest.mark.slow
def test_sync_perf_100mb_file(fresh_db):
    """Performance test: sync a 100MB file (~233,017 slices)."""
    _run_file_sync_test(fresh_db, file_size_bytes=100 * 1024 * 1024)


@pytest.mark.slow
def test_sync_perf_200mb_file(fresh_db):
    """Performance test: sync a 200MB file (~466,034 slices)."""
    _run_file_sync_test(fresh_db, file_size_bytes=200 * 1024 * 1024)


@pytest.mark.slow
def test_sync_perf_comparison():
    """Run all file sizes and compare performance."""
    import sqlite3
    from core.db import Database
    from core import schema, transport

    results = []

    for size_mb in [1, 10]:  # Skip 100MB for quick comparison
        print(f"\n\n{'#'*70}")
        print(f"# Testing {size_mb}MB file")
        print(f"{'#'*70}")

        # Create fresh database for each test
        conn = sqlite3.Connection(":memory:")
        db = Database(conn)
        schema.create_all(db)
        transport.reset()
        transport.enable_loopback()

        try:
            result = _run_file_sync_test(db, file_size_bytes=size_mb * 1024 * 1024)
            results.append(result)
        except Exception as e:
            print(f"FAILED: {e}")
            results.append({'file_size': size_mb * 1024 * 1024, 'error': str(e)})

    # Summary
    print(f"\n\n{'='*70}")
    print("PERFORMANCE COMPARISON SUMMARY")
    print(f"{'='*70}")
    print(f"{'Size':<10} {'Slices':>12} {'Sync Time':>12} {'ms/slice':>12}")
    print(f"{'-'*70}")

    for r in results:
        if 'error' in r:
            size_mb = r['file_size'] // (1024 * 1024)
            print(f"{size_mb}MB{'':<7} {'ERROR':>12} {r['error'][:40]}")
        else:
            size_mb = r['file_size'] // (1024 * 1024)
            ms_per_slice = r['sync_time'] * 1000 / r['slice_count']
            print(f"{size_mb}MB{'':<7} {r['slice_count']:>12,} {r['sync_time']:>11.1f}s {ms_per_slice:>12.3f}")

    print(f"{'='*70}")
