"""
Performance tests for large file sync.

Tests that file sync completes in a reasonable number of rounds for:
- 1MB file (~2222 slices at 450 bytes/slice)
- 10MB file (~23333 slices)
- 100MB file (~233333 slices)

The key metric is bisection depth - with the new time-based unified key design,
we should need far fewer rounds than with the old unbounded 63-bit range.

Expected rounds (post-fix):
- 1MB: ~50-100 rounds
- 10MB: ~200-300 rounds
- 100MB: ~300-500 rounds

Run with: PYTHONPATH=. pytest tests/test_sync_perf_files.py -v -s -m slow
"""
import pytest
import time
from events.identity import user, invite, peer
from events.content import message, message_attachment
from tests.utils import tick_helper
from core import tick


def _create_file_data(size_bytes: int) -> bytes:
    """Create test file data of specified size."""
    # Create repeating pattern that's compressible but still realistic
    pattern = b'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\n' * 100
    repetitions = (size_bytes // len(pattern)) + 1
    return (pattern * repetitions)[:size_bytes]


def _run_file_sync_test(fresh_db, file_size_bytes: int, max_rounds: int, expected_max_rounds: int):
    """Run a file sync test and report performance metrics."""
    db = fresh_db
    size_name = f"{file_size_bytes // (1024*1024)}MB" if file_size_bytes >= 1024*1024 else f"{file_size_bytes // 1024}KB"

    print(f"\n{'='*60}")
    print(f"File Sync Performance Test: {size_name} ({file_size_bytes:,} bytes)")
    print(f"{'='*60}")

    # Setup: Alice creates network, Bob joins
    print("\n[Setup] Creating network...")
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)

    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    db.commit()

    # Initial sync
    print("[Setup] Running initial sync...")
    t_ms = tick_helper.initial_sync(db, start_t_ms=None)

    # Alice creates message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content=f'Here is a {size_name} file',
        t_ms=t_ms,
        db=db
    )
    message_id = msg_result['id']
    t_ms += 100

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
        t_ms=t_ms,
        db=db
    )
    file_id = file_result['file_id']
    slice_count = file_result['slice_count']
    create_time = time.time() - start_create
    print(f"[Create] Created file: {slice_count:,} slices in {create_time:.2f}s")
    db.commit()

    t_ms += 100

    # Track sync progress
    print(f"\n[Sync] Starting sync (max {max_rounds} rounds)...")
    start_sync = time.time()

    rounds_completed = 0
    last_progress_pct = -1
    sync_complete = False

    for round_num in range(max_rounds):
        tick.tick(t_ms=t_ms + round_num * 100, db=db)
        rounds_completed = round_num + 1

        # Check progress every 10 rounds or at key milestones
        if round_num % 10 == 0 or round_num < 5:
            progress = message_attachment.get_file_download_progress(file_id, bob['peer_id'], db)
            if progress:
                pct = progress['percentage_complete']
                if pct != last_progress_pct:
                    elapsed = time.time() - start_sync
                    print(f"  Round {round_num:4d}: {progress['slices_received']:6,}/{slice_count:,} slices ({pct:3d}%) - {elapsed:.1f}s")
                    last_progress_pct = pct

                if progress['is_complete']:
                    sync_complete = True
                    break

    sync_time = time.time() - start_sync

    # Verify completion
    print(f"\n[Verify] Checking sync completion...")
    progress = message_attachment.get_file_download_progress(file_id, bob['peer_id'], db)

    if progress and progress['is_complete']:
        print(f"  Bob has all {progress['slices_received']:,} slices")

        # Verify data integrity
        bob_data = message_attachment.get_file_data(file_id, bob['peer_id'], db)
        if bob_data == file_data:
            print(f"  Data integrity verified (matches original)")
        else:
            print(f"  WARNING: Data mismatch!")
            assert False, "Data integrity check failed"
    else:
        slices_received = progress['slices_received'] if progress else 0
        pct = (slices_received / slice_count * 100) if slice_count > 0 else 0
        print(f"  INCOMPLETE: {slices_received:,}/{slice_count:,} slices ({pct:.1f}%)")

    # Report metrics
    print(f"\n{'='*60}")
    print(f"Results: {size_name} File Sync")
    print(f"{'='*60}")
    print(f"  File size:      {file_size_bytes:,} bytes ({slice_count:,} slices)")
    print(f"  Rounds used:    {rounds_completed:,} / {max_rounds:,}")
    print(f"  Sync time:      {sync_time:.2f}s")
    print(f"  Create time:    {create_time:.2f}s")
    print(f"  Slices/round:   {slice_count / rounds_completed:.1f}")
    print(f"  Complete:       {'YES' if sync_complete else 'NO'}")
    print(f"{'='*60}")

    # Assert completion within expected rounds
    assert sync_complete, f"Sync did not complete within {max_rounds} rounds"
    assert rounds_completed <= expected_max_rounds, \
        f"Sync took {rounds_completed} rounds, expected <= {expected_max_rounds}"

    return {
        'file_size': file_size_bytes,
        'slice_count': slice_count,
        'rounds': rounds_completed,
        'sync_time': sync_time,
        'create_time': create_time,
    }


@pytest.mark.slow
def test_sync_perf_1mb_file(fresh_db):
    """Performance test: sync a 1MB file.

    1MB = 1,048,576 bytes / 450 bytes per slice = 2,330 slices

    With time-based unified keys, this should sync in ~50-100 rounds.
    """
    _run_file_sync_test(
        fresh_db,
        file_size_bytes=1 * 1024 * 1024,  # 1MB
        max_rounds=200,
        expected_max_rounds=150,
    )


@pytest.mark.slow
def test_sync_perf_10mb_file(fresh_db):
    """Performance test: sync a 10MB file.

    10MB = 10,485,760 bytes / 450 bytes per slice = 23,302 slices

    This was the problem case - used to get stuck at 99%.
    With time-based unified keys, should sync in ~200-300 rounds.
    """
    _run_file_sync_test(
        fresh_db,
        file_size_bytes=10 * 1024 * 1024,  # 10MB
        max_rounds=600,
        expected_max_rounds=400,
    )


@pytest.mark.slow
def test_sync_perf_100mb_file(fresh_db):
    """Performance test: sync a 100MB file.

    100MB = 104,857,600 bytes / 450 bytes per slice = 233,017 slices

    With time-based unified keys, should sync in ~300-500 rounds.
    """
    _run_file_sync_test(
        fresh_db,
        file_size_bytes=100 * 1024 * 1024,  # 100MB
        max_rounds=1000,
        expected_max_rounds=700,
    )


@pytest.mark.slow
def test_sync_perf_comparison():
    """Run all file sizes and compare performance.

    This test provides a summary comparison across file sizes.
    """
    import sqlite3
    from core.db import Database
    from core import schema

    results = []

    for size_mb in [1, 10]:  # Skip 100MB for quick comparison
        print(f"\n\n{'#'*70}")
        print(f"# Testing {size_mb}MB file")
        print(f"{'#'*70}")

        # Create fresh database for each test
        conn = sqlite3.Connection(":memory:")
        db = Database(conn)
        schema.create_all(db)

        try:
            result = _run_file_sync_test(
                db,
                file_size_bytes=size_mb * 1024 * 1024,
                max_rounds=600,
                expected_max_rounds=500,
            )
            results.append(result)
        except Exception as e:
            print(f"FAILED: {e}")
            results.append({'file_size': size_mb * 1024 * 1024, 'error': str(e)})

    # Summary
    print(f"\n\n{'='*70}")
    print("PERFORMANCE COMPARISON SUMMARY")
    print(f"{'='*70}")
    print(f"{'Size':<10} {'Slices':>10} {'Rounds':>10} {'Time':>10} {'Slices/Round':>15}")
    print(f"{'-'*70}")

    for r in results:
        if 'error' in r:
            size_mb = r['file_size'] // (1024 * 1024)
            print(f"{size_mb}MB{'':<7} {'ERROR':>10} {r['error'][:40]}")
        else:
            size_mb = r['file_size'] // (1024 * 1024)
            slices_per_round = r['slice_count'] / r['rounds']
            print(f"{size_mb}MB{'':<7} {r['slice_count']:>10,} {r['rounds']:>10,} {r['sync_time']:>9.1f}s {slices_per_round:>15.1f}")

    print(f"{'='*70}")
