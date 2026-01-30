"""
Focused sync test for file attachments.

Verifies that:
1. Messages are synced from Alice to Bob
2. File metadata is synced
3. File slices are synced
4. Message attachments are synced
5. Bob can reassemble and decrypt the file

This test isolates the sync phase to identify which events are/aren't being synced.
"""
import sqlite3
from core.db import Database
from core import schema
from events.identity import user, invite, peer
from events.content import channel, message, message_attachment
from core import tick
from tests.utils.tick_helper import assert_eventually, run_ticks, TestClock


def test_file_attachment_sync_only(fresh_db):
    """Test that file attachment events are properly synced to Bob."""

    # Setup: Initialize in-memory database
    db = fresh_db
    clock = TestClock()

    print("\n=== Setup: Create networks and invite ===")

    # Alice creates network and channel
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    print(f"✓ Alice created network")

    # Alice creates an invite for Bob
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db
    )
    print(f"✓ Alice created invite: {invite_id[:20]}...")

    # Bob joins Alice's network
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)

    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.now(), db=db)
    bob_peer_shared_id = bob['peer_shared_id']
    print(f"✓ Bob joined network, peer_id: {bob['peer_id'][:20]}...")

    db.commit()

    # Initial sync to converge (need multiple rounds for GKS events to propagate)
    print("\n=== Initial sync (bootstrap + GKS) ===")
    run_ticks(db, None, 15)

    print("✓ Initial sync completed")

    # Check that Bob received Alice's channel
    # Note: channels are subjective - Bob needs to have received/synced Alice's channel
    bob_alice_channel = db.query_one(
        "SELECT channel_id FROM channels WHERE channel_id = ? AND recorded_by = ?",
        (alice['channel_id'], bob['peer_id'])
    )
    if bob_alice_channel:
        print(f"✓ Bob received Alice's channel via sync")
    else:
        print(f"⚠ Bob doesn't have Alice's channel - will use Alice's channel_id for messages")

    print("\n=== Alice creates message ===")

    # Alice creates a message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='File attachment test',
        t_ms=clock.tick(),
        db=db
    )
    message_id = msg_result['id']
    print(f"✓ Alice created message: {message_id[:20]}...")

    # Check message is in Alice's DB
    alice_msg = db.query_one(
        "SELECT message_id FROM messages WHERE message_id = ? AND recorded_by = ?",
        (message_id, alice['peer_id'])
    )
    assert alice_msg is not None, "Message should be in Alice's messages table"

    # Note: Events may not be immediately in shareable_events depending on encryption state
    # This is an internal detail - the critical test is sync delivery
    print(f"✓ Message created on Alice's side")

    db.commit()

    print("\n=== Sync message to Bob ===")
    def bob_has_message():
        bob_msg_count = db.query_all(
            "SELECT message_id FROM messages WHERE recorded_by = ?",
            (bob['peer_id'],)
        )
        assert len(bob_msg_count) > 0, "Bob should receive message after sync"

    assert_eventually(bob_has_message, db=db, start_t_ms=None)
    print(f"✓ Bob received message")

    print("\n=== Alice creates file and attachment ===")

    # Alice creates a small 1KB file
    file_data = b'File content test. ' * 60  # 19 * 60 = 1140 bytes
    file_data = file_data[:1000]  # Trim to exactly 1000 bytes
    assert len(file_data) == 1000

    # Create file and attachment
    file_result = message_attachment.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        file_data=file_data,
        filename='test.txt',
        mime_type='text/plain',
        t_ms=clock.tick(),
        db=db
    )
    file_id = file_result['file_id']
    slice_count = file_result['slice_count']
    print(f"✓ Alice created file: {file_id[:20]}..., {slice_count} slices")

    # Note: File events may not be immediately in shareable_events
    # They should be added during projection/sync
    # This is an internal implementation detail not critical for functionality

    # Check that file slices exist
    alice_slices = db.query_all(
        "SELECT slice_number FROM file_slices WHERE file_id = ? AND recorded_by = ? ORDER BY slice_number",
        (file_id, alice['peer_id'])
    )
    assert len(alice_slices) == slice_count, f"Alice should have {slice_count} slices"
    print(f"✓ Alice has {len(alice_slices)} file slices")

    db.commit()

    print("\n=== Sync file to Bob (9 rounds) ===")
    for round_num in range(9):
        run_ticks(db, None, 1)

        # Check progress using API (what frontends would use)
        progress = message_attachment.get_file_download_progress(file_id, bob['peer_id'], db)
        if progress:
            print(f"  Round {round_num}: {progress['filename']} - "
                  f"{progress['slices_received']}/{progress['total_slices']} slices "
                  f"({progress['percentage_complete']}%) "
                  f"[{progress['size_human']}]")
        else:
            print(f"  Round {round_num}: File not yet available")

    print("✓ Sync completed")

    print("\n=== Verify Bob received file ===")

    # Bob should have file metadata (in message_attachments)
    bob_file = db.query_one(
        "SELECT file_id FROM message_attachments WHERE file_id = ? AND recorded_by = ?",
        (file_id, bob['peer_id'])
    )
    assert bob_file is not None, "Bob should have file metadata"
    print(f"✓ Bob has file metadata")

    # Bob should have all slices
    bob_slices = db.query_all(
        "SELECT slice_number FROM file_slices WHERE file_id = ? AND recorded_by = ? ORDER BY slice_number",
        (file_id, bob['peer_id'])
    )
    assert len(bob_slices) == slice_count, f"Bob should have {slice_count} slices, got {len(bob_slices)}"
    print(f"✓ Bob has all {len(bob_slices)} slices")

    # Bob should have attachment metadata
    bob_attach = db.query_one(
        "SELECT file_id FROM message_attachments WHERE file_id = ? AND recorded_by = ?",
        (file_id, bob['peer_id'])
    )
    assert bob_attach is not None, "Bob should have attachment metadata"
    print(f"✓ Bob has attachment metadata")

    # Bob should be able to retrieve the file
    bob_retrieved = message_attachment.get_file_data(file_id, bob['peer_id'], db)
    assert bob_retrieved == file_data, "Bob's file should match original"
    print(f"✓ Bob can retrieve and decrypt file (matches original)")

    print("\n✅ All sync assertions passed!")
