"""
Debug test: file slice syncing - check each assertion carefully.

Proceeds step-by-step to identify exactly where syncing breaks down.
"""
import sqlite3
from db import Database
import schema
from events.identity import user, invite
from events.content import message, file
from events.transit import sync


def test_file_slice_sync_debug():
    """Step through file slice sync with careful assertions."""

    # Setup
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    print("\n=== STEP 1: Create Alice ===")
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    print(f"✓ Alice peer_id: {alice['peer_id'][:20]}...")
    assert alice['peer_id']
    assert alice['channel_id']

    print("\n=== STEP 2: Create invite and Bob joins ===")
    invite_id, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    print(f"✓ Bob peer_id: {bob['peer_id'][:20]}...")
    print(f"✓ Bob channel_id: {bob['channel_id'][:20]}...")
    db.commit()

    print("\n=== STEP 3: Initial sync (2 rounds) ===")
    for round_num in range(2):
        sync.send_request_to_all(t_ms=2100 + round_num * 100, db=db)
        db.commit()
        sync.receive(batch_size=20, t_ms=2200 + round_num * 100, db=db)
        db.commit()
    print("✓ Initial sync completed")

    print("\n=== STEP 4: Alice creates message ===")
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Check out this file!',
        t_ms=3000,
        db=db
    )
    message_id = msg_result['id']
    print(f"✓ Message created: {message_id[:20]}...")
    db.commit()

    print("\n=== STEP 5: Check Alice's messages ===")
    alice_msgs = message.list_messages(alice['channel_id'], alice['peer_id'], db)
    assert len(alice_msgs) == 1, f"Alice should have 1 message, got {len(alice_msgs)}"
    print(f"✓ Alice sees message in her channel")

    print("\n=== STEP 6: Alice creates file ===")
    file_data = b'This is a test file. ' * 100
    file_data = file_data[:2000]

    file_result = file.create_with_attachment(
        peer_id=alice['peer_id'],
        message_id=message_id,
        file_data=file_data,
        filename='test_file.txt',
        mime_type='text/plain',
        t_ms=4000,
        db=db
    )
    file_id = file_result['file_id']
    slice_count = file_result['slice_count']
    print(f"✓ File created: {file_id[:20]}..., {slice_count} slices")
    assert slice_count == 5, f"Expected 5 slices, got {slice_count}"
    db.commit()

    print("\n=== STEP 7: Check Alice's shareable_events for file ===")
    alice_shareable_file = db.query_one(
        "SELECT event_id FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
        (file_id, alice['peer_id'])
    )
    assert alice_shareable_file is not None, f"File {file_id[:20]}... should be in Alice's shareable_events"
    print(f"✓ File is in Alice's shareable_events")

    print("\n=== STEP 8: Check Alice's shareable_events for file slices ===")
    alice_slices = db.query_all(
        "SELECT file_slice_id FROM file_slices WHERE file_id = ? AND recorded_by = ? ORDER BY slice_number",
        (file_id, alice['peer_id'])
    )
    print(f"Alice has {len(alice_slices)} slices")
    assert len(alice_slices) == 5, f"Expected 5 slices, got {len(alice_slices)}"

    for i, slice_row in enumerate(alice_slices):
        slice_id = slice_row['file_slice_id']
        slice_shareable = db.query_one(
            "SELECT event_id FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
            (slice_id, alice['peer_id'])
        )
        assert slice_shareable is not None, f"Slice {i} {slice_id[:20]}... should be in shareable_events"
    print(f"✓ All {len(alice_slices)} slices are in Alice's shareable_events")

    print("\n=== STEP 9: Check Alice's shareable_events for message_attachment ===")
    alice_attachments = db.query_all(
        "SELECT message_attachment_id FROM message_attachments WHERE file_id = ? AND recorded_by = ?",
        (file_id, alice['peer_id'])
    )
    assert len(alice_attachments) == 1, f"Expected 1 attachment, got {len(alice_attachments)}"

    attachment_id = alice_attachments[0]['message_attachment_id']
    attachment_shareable = db.query_one(
        "SELECT event_id FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
        (attachment_id, alice['peer_id'])
    )
    assert attachment_shareable is not None, f"Attachment {attachment_id[:20]}... should be in shareable_events"
    print(f"✓ Message attachment is in Alice's shareable_events")

    print("\n=== STEP 10: Sync to Bob (5 rounds) ===")
    for round_num in range(5):
        sync.send_request_to_all(t_ms=5000 + round_num * 100, db=db)
        db.commit()
        sync.receive(batch_size=50, t_ms=5050 + round_num * 100, db=db)
        db.commit()

        # Check progress after each round
        bob_msg_count = len(message.list_messages(bob['channel_id'], bob['peer_id'], db))
        bob_file = db.query_one(
            "SELECT file_id FROM files WHERE file_id = ? AND recorded_by = ?",
            (file_id, bob['peer_id'])
        )
        bob_slices = db.query_all(
            "SELECT file_slice_id FROM file_slices WHERE file_id = ? AND recorded_by = ?",
            (file_id, bob['peer_id'])
        )
        print(f"  Round {round_num}: message={'✓' if bob_msg_count > 0 else '✗'}, file={'✓' if bob_file else '✗'}, slices={len(bob_slices)}/{slice_count}")

    print("✓ Sync completed")

    print("\n=== STEP 11: Check Bob's messages ===")
    bob_msgs = message.list_messages(bob['channel_id'], bob['peer_id'], db)
    print(f"Bob has {len(bob_msgs)} messages (expected 1)")
    assert len(bob_msgs) == 1, f"Bob should have 1 message, got {len(bob_msgs)}"
    print(f"✓ Bob received message")

    print("\n=== STEP 12: Check Bob's file metadata ===")
    bob_file = db.query_one(
        "SELECT file_id FROM files WHERE file_id = ? AND recorded_by = ?",
        (file_id, bob['peer_id'])
    )
    assert bob_file is not None, f"Bob should have file {file_id[:20]}..."
    print(f"✓ Bob received file metadata")

    print("\n=== STEP 13: Check Bob's file slices ===")
    bob_slices = db.query_all(
        "SELECT file_slice_id FROM file_slices WHERE file_id = ? AND recorded_by = ? ORDER BY slice_number",
        (file_id, bob['peer_id'])
    )
    print(f"Bob has {len(bob_slices)} slices (expected {slice_count})")
    assert len(bob_slices) == slice_count, f"Bob should have {slice_count} slices, got {len(bob_slices)}"
    print(f"✓ Bob received all {slice_count} slices")

    print("\n=== STEP 14: Check Bob's attachment metadata ===")
    bob_attachments = db.query_all(
        "SELECT message_attachment_id FROM message_attachments WHERE file_id = ? AND recorded_by = ?",
        (file_id, bob['peer_id'])
    )
    assert len(bob_attachments) == 1, f"Bob should have 1 attachment, got {len(bob_attachments)}"
    print(f"✓ Bob received attachment metadata")

    print("\n=== STEP 15: Bob can retrieve file ===")
    bob_retrieved = file.get_file(file_id, bob['peer_id'], db)
    assert bob_retrieved is not None, "Bob should be able to retrieve file"
    assert bob_retrieved == file_data, "Bob's file should match original"
    print(f"✓ Bob can retrieve and decrypt file")

    print("\n✅ All assertions passed!")


if __name__ == '__main__':
    test_file_slice_sync_debug()
