"""
Scenario test: Alice attaches a file to a message, Bob receives it.

Tests the complete file attachment flow:
1. Alice creates network, Bob joins via invite
2. Alice creates a 2KB file and attaches to message (5 slices × 450 bytes)
3. Verify file metadata, slices, and encryption
4. Alice can retrieve and decrypt file
5. Sync: Bob receives file slices, descriptor, and attachment
6. Bob can retrieve and decrypt file
7. Verify root_hash integrity check
8. Test reprojection and convergence

Tests the API-only (no direct DB inspection except via query functions).
"""
import sqlite3
from db import Database
import schema
from events.identity import user, invite
from events.content import channel, message, file
from events.transit import sync
import crypto


def test_two_party_file_attachment_and_sync():
    """Complete file attachment flow with two peers and sync."""

    # Setup: Initialize in-memory database
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    print("\n=== Setup: Create networks and invite ===")

    # Alice creates network and channel
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    print(f"✓ Alice created network")

    # Alice creates an invite for Bob
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )
    print(f"✓ Alice created invite: {invite_id[:20]}...")

    # Bob joins Alice's network
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    print(f"✓ Bob joined network, peer_id: {bob['peer_id'][:20]}...")

    db.commit()

    # Initial sync to converge (need multiple rounds for GKS events to propagate)
    for i in range(5):
        print(f"\n=== Sync Round {i+1} ===")
        sync.send_request_to_all(t_ms=2100 + i*200, db=db)
        db.commit()
        sync.receive(batch_size=20, t_ms=2200 + i*200, db=db)
        db.commit()

    print("✓ Initial sync completed")

    print("\n=== Alice creates message with file attachment ===")

    # Alice sends initial message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Check out this file!',
        t_ms=3000,
        db=db
    )
    message_id = msg_result['id']
    print(f"✓ Alice created message: {message_id[:20]}...")

    # Alice creates a 2KB file (will be 5 slices of 450 bytes + 50 bytes last slice)
    file_data = b'This is a test file. ' * 100  # Repeat to create ~2KB
    file_data = file_data[:2000]  # Exact 2000 bytes
    assert len(file_data) == 2000

    # Alice attaches file to message
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
    print(f"✓ Alice created file: {file_id[:20]}..., {slice_count} slices")

    # Expected: 2000 bytes / 450 bytes per slice = 4.44 → 5 slices
    assert slice_count == 5, f"Expected 5 slices, got {slice_count}"

    # Verify Alice can retrieve the file
    alice_retrieved = file.get_file(file_id, alice['peer_id'], db)
    assert alice_retrieved is not None
    assert alice_retrieved == file_data
    print(f"✓ Alice retrieved file successfully, matches original")

    # Verify attachment appears in message query
    alice_messages = message.list_messages(alice['channel_id'], alice['peer_id'], db)
    assert len(alice_messages) == 1
    assert len(alice_messages[0]['attachments']) == 1
    attachment = alice_messages[0]['attachments'][0]
    assert attachment['file_id'] == file_id
    assert attachment['filename'] == 'test_file.txt'
    assert attachment['mime_type'] == 'text/plain'
    assert attachment['blob_bytes'] == 2000
    print(f"✓ Attachment visible in Alice's message query")

    db.commit()

    print("\n=== Test file encryption ===")

    # Verify that slices are properly encrypted (not plaintext)
    alice_slice_row = db.query_one(
        "SELECT ciphertext FROM file_slices WHERE file_id = ? AND slice_number = 0 AND recorded_by = ?",
        (file_id, alice['peer_id'])
    )
    assert alice_slice_row is not None
    assert alice_slice_row['ciphertext'] != file_data[:450]
    print(f"✓ File slices are encrypted (ciphertext ≠ plaintext)")

    # Verify root_hash computation
    alice_file_meta = db.query_one(
        "SELECT root_hash FROM files WHERE file_id = ? AND recorded_by = ?",
        (file_id, alice['peer_id'])
    )
    assert alice_file_meta is not None

    # Manually compute expected root_hash
    alice_slices = db.query_all(
        "SELECT ciphertext FROM file_slices WHERE file_id = ? AND recorded_by = ? ORDER BY slice_number",
        (file_id, alice['peer_id'])
    )
    slice_ciphertexts = [s['ciphertext'] for s in alice_slices]
    expected_root_hash = crypto.compute_root_hash(slice_ciphertexts)
    assert alice_file_meta['root_hash'] == expected_root_hash
    print(f"✓ Root hash matches (integrity verified)")

    print("\n=== Sync file to Bob ===")

    # Sync file events to Bob (need multiple rounds for message + file + slices + attachment)
    # More rounds needed because: round 1 sends events, round 2 may trigger key shares, round 3-5 complete transfer
    for round_num in range(5):
        sync.send_request_to_all(t_ms=5000 + round_num * 100, db=db)
        db.commit()
        sync.receive(batch_size=50, t_ms=5050 + round_num * 100, db=db)
        db.commit()

    print("✓ Sync completed")

    print("\n=== Verify Bob received file ===")

    # Bob should see the message with attachment
    bob_messages = message.list_messages(bob['channel_id'], bob['peer_id'], db)
    print(f"Bob sees {len(bob_messages)} messages")
    assert len(bob_messages) == 1, f"Expected 1 message for Bob, got {len(bob_messages)}"

    bob_msg = bob_messages[0]
    assert bob_msg['content'] == 'Check out this file!'
    assert len(bob_msg['attachments']) == 1, f"Expected 1 attachment, got {len(bob_msg['attachments'])}"

    bob_attachment = bob_msg['attachments'][0]
    assert bob_attachment['file_id'] == file_id
    assert bob_attachment['filename'] == 'test_file.txt'
    assert bob_attachment['mime_type'] == 'text/plain'
    assert bob_attachment['blob_bytes'] == 2000
    print(f"✓ Bob sees message with attachment metadata")

    # Bob should be able to retrieve and decrypt the file
    bob_retrieved = file.get_file(file_id, bob['peer_id'], db)
    assert bob_retrieved is not None, "Bob should be able to retrieve the file"
    assert bob_retrieved == file_data, "Bob's retrieved file should match original"
    print(f"✓ Bob retrieved and decrypted file successfully, matches original")

    # Verify Bob has all slices
    bob_slices = db.query_all(
        "SELECT slice_number FROM file_slices WHERE file_id = ? AND recorded_by = ? ORDER BY slice_number",
        (file_id, bob['peer_id'])
    )
    assert len(bob_slices) == 5, f"Bob should have 5 slices, got {len(bob_slices)}"
    print(f"✓ Bob has all {len(bob_slices)} file slices")

    print("\n=== Test reprojection ===")

    # Verify reprojection (can rebuild all state from events)
    from tests.utils import assert_reprojection
    assert_reprojection(db)
    print(f"✓ Reprojection tests passed")

    print("\n=== Test convergence ===")

    # Verify convergence (different order of events produces same state)
    from tests.utils import assert_convergence
    assert_convergence(db)
    print(f"✓ Convergence tests passed")

    print("\n✅ All tests passed! File attachment, encryption, sync, and retrieval work correctly.")


if __name__ == '__main__':
    test_two_party_file_attachment_and_sync()
