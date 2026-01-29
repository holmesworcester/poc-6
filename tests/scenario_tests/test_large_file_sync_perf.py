"""
Scenario test: Large file sync performance (loopback).

Creates a 10MB file attachment, then times sync completion to Bob.
Uses direct event commands (no API client) and loopback transport.
"""
import time

from core import tick, transport
from events.identity import user, invite, peer
from events.content import message, message_attachment
from tests.utils import tick_helper


FILE_BYTES = 10 * 1024 * 1024  # 10MB


def test_large_file_sync_perf_loopback(fresh_db):
    db = fresh_db

    # Ensure loopback mode (no simulator)
    transport.enable_loopback()

    # Setup network + peers
    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    _invite_id, invite_link, _invite_data = invite.create(
        peer_id=alice["peer_id"],
        t_ms=1500,
        db=db,
    )
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name="Bob", t_ms=2000, db=db)
    db.commit()

    # Initial sync to converge (GKS + connection)
    t_ms = tick_helper.initial_sync(db, start_t_ms=None)

    # Alice creates message
    msg_result = message.create(
        peer_id=alice["peer_id"],
        channel_id=alice["channel_id"],
        content="Large file sync perf test",
        t_ms=3000,
        db=db,
    )
    message_id = msg_result["id"]

    # Alice creates 10MB attachment
    file_data = b"X" * FILE_BYTES
    file_result = message_attachment.create(
        peer_id=alice["peer_id"],
        message_id=message_id,
        file_data=file_data,
        filename="large.bin",
        mime_type="application/octet-stream",
        t_ms=4000,
        db=db,
    )
    file_id = file_result["file_id"]
    total_slices = file_result["slice_count"]
    db.commit()

    print(f"Created 10MB file with {total_slices} slices")

    # Time sync only (exclude write time)
    # Ensure monotonic time (after attachment creation at t_ms=4000)
    t_ms = max(t_ms, 5000)
    start = time.perf_counter()
    max_rounds = 2000  # 200s of simulated time at 100ms per tick
    completed = False

    for round_num in range(max_rounds):
        t_ms = t_ms + tick_helper.TICK_INTERVAL_MS
        tick.tick(t_ms=t_ms, db=db)

        progress = message_attachment.get_file_download_progress(file_id, bob["peer_id"], db)
        if progress and progress["slices_received"] >= progress["total_slices"]:
            completed = True
            break

        if progress and round_num % 50 == 0:
            print(
                f"Round {round_num}: {progress['slices_received']}/"
                f"{progress['total_slices']} slices ({progress['percentage_complete']}%)"
            )

    elapsed = time.perf_counter() - start

    assert completed, "10MB file should sync within max rounds on loopback"

    mib = FILE_BYTES / (1024 * 1024)
    mib_per_sec = mib / elapsed if elapsed > 0 else 0.0
    print(f"Sync complete in {elapsed:.2f}s, throughput ~{mib_per_sec:.2f} MiB/s")

    # Verify file integrity
    bob_file_data = message_attachment.get_file_data(file_id, bob["peer_id"], db)
    assert bob_file_data == file_data
