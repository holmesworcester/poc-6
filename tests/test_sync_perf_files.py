"""
Performance tests for file sync.

Run with: pytest -m slow tests/test_sync_perf_files.py -v -s
"""
import pytest
from events.identity import user, invite, peer
from events.content import message, message_attachment
from tests.utils.tick_helper import run_ticks


@pytest.mark.slow
def test_sync_perf_1mb_file(fresh_db):
    """Perf: sync 1MB file between two peers.

    Expected: ~120 rounds, ~2s
    """
    db = fresh_db
    file_size = 1 * 1024 * 1024  # 1MB

    # Setup
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    db.commit()

    # Initial sync
    run_ticks(db=db, start_t_ms=3000, num_rounds=15)

    # Create message with file
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='1MB file test',
        t_ms=5000,
        db=db
    )

    file_data = b'X' * file_size
    file_result = message_attachment.create(
        peer_id=alice['peer_id'],
        message_id=msg_result['id'],
        file_data=file_data,
        filename='test_1mb.bin',
        mime_type='application/octet-stream',
        t_ms=6000,
        db=db
    )
    db.commit()

    file_id = file_result['file_id']
    total_slices = file_result['slice_count']
    print(f"\nCreated 1MB file: {total_slices} slices")

    # Sync until complete
    max_rounds = 200
    for round_num in range(max_rounds):
        from core import tick
        tick.tick(t_ms=10000 + round_num * 100, db=db)
        db.commit()

        progress = message_attachment.get_file_download_progress(file_id, bob['peer_id'], db)
        if progress and progress['is_complete']:
            print(f"Sync complete: {round_num + 1} rounds")
            break

        if round_num % 50 == 0:
            slices = progress['slices_received'] if progress else 0
            print(f"Round {round_num}: {slices}/{total_slices} slices")

    # Verify
    assert progress is not None, "Should have progress"
    assert progress['is_complete'], f"Should complete within {max_rounds} rounds"

    # Verify data integrity
    retrieved = message_attachment.get_file_data(file_id, bob['peer_id'], db)
    assert retrieved == file_data, "File data should match"
    print(f"✓ 1MB file synced and verified")


@pytest.mark.slow
def test_sync_perf_10mb_file(fresh_db):
    """Perf: sync 10MB file between two peers.

    Expected: ~250 rounds, ~70s (needs optimization)
    """
    db = fresh_db
    file_size = 10 * 1024 * 1024  # 10MB

    # Setup
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    db.commit()

    # Initial sync
    run_ticks(db=db, start_t_ms=3000, num_rounds=15)

    # Create message with file
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='10MB file test',
        t_ms=5000,
        db=db
    )

    file_data = b'Y' * file_size
    file_result = message_attachment.create(
        peer_id=alice['peer_id'],
        message_id=msg_result['id'],
        file_data=file_data,
        filename='test_10mb.bin',
        mime_type='application/octet-stream',
        t_ms=6000,
        db=db
    )
    db.commit()

    file_id = file_result['file_id']
    total_slices = file_result['slice_count']
    print(f"\nCreated 10MB file: {total_slices} slices")

    # Sync until complete - 10MB needs more rounds
    # With cursor-based streaming, should complete in ~300-400 rounds
    max_rounds = 600
    for round_num in range(max_rounds):
        from core import tick
        tick.tick(t_ms=10000 + round_num * 100, db=db)
        db.commit()

        progress = message_attachment.get_file_download_progress(file_id, bob['peer_id'], db)
        if progress and progress['is_complete']:
            print(f"Sync complete: {round_num + 1} rounds")
            break

        if round_num % 50 == 0:
            slices = progress['slices_received'] if progress else 0
            pct = (slices * 100 // total_slices) if total_slices else 0
            print(f"Round {round_num}: {pct}% ({slices}/{total_slices} slices)")

    # Verify
    assert progress is not None, "Should have progress"
    assert progress['is_complete'], f"Should complete within {max_rounds} rounds"

    # Verify data integrity
    retrieved = message_attachment.get_file_data(file_id, bob['peer_id'], db)
    assert retrieved == file_data, "File data should match"
    print(f"✓ 10MB file synced and verified")


@pytest.mark.slow
def test_sync_perf_100mb_file(fresh_db):
    """Perf: sync 100MB file between two peers.

    Expected: ~200 rounds with bulk transfer optimization
    """
    db = fresh_db
    file_size = 100 * 1024 * 1024  # 100MB

    # Setup
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    db.commit()

    # Initial sync
    run_ticks(db=db, start_t_ms=3000, num_rounds=15)

    # Create message with file
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='100MB file test',
        t_ms=5000,
        db=db
    )

    file_data = b'Z' * file_size
    file_result = message_attachment.create(
        peer_id=alice['peer_id'],
        message_id=msg_result['id'],
        file_data=file_data,
        filename='test_100mb.bin',
        mime_type='application/octet-stream',
        t_ms=6000,
        db=db
    )
    db.commit()

    file_id = file_result['file_id']
    total_slices = file_result['slice_count']
    print(f"\nCreated 100MB file: {total_slices} slices")

    # Sync until complete
    max_rounds = 1000
    for round_num in range(max_rounds):
        from core import tick
        tick.tick(t_ms=10000 + round_num * 100, db=db)
        db.commit()

        progress = message_attachment.get_file_download_progress(file_id, bob['peer_id'], db)
        if progress and progress['is_complete']:
            print(f"Sync complete: {round_num + 1} rounds")
            break

        if round_num % 200 == 0:
            slices = progress['slices_received'] if progress else 0
            pct = (slices * 100 // total_slices) if total_slices else 0
            print(f"Round {round_num}: {pct}% ({slices}/{total_slices} slices)")

    # Verify
    assert progress is not None, "Should have progress"
    assert progress['is_complete'], f"Should complete within {max_rounds} rounds"

    # Verify data integrity
    retrieved = message_attachment.get_file_data(file_id, bob['peer_id'], db)
    assert retrieved == file_data, "File data should match"
    print(f"✓ 100MB file synced and verified")
