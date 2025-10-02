"""Test invite creation, link encoding/decoding, and user event validation."""
import pytest
import sqlite3
import json
import base64
import time

import schema
import crypto
import store as store_module
from db import Database
from events import peer, peer_shared, key, group, invite, user


def test_invite_roundtrip():
    """Test complete invite flow: create invite -> decode link -> create user -> validate and project.

    This test simulates:
    1. Alice creates network and invite
    2. Alice sends invite link to Bob (out of band)
    3. Bob decodes link, creates user event with invite proof
    4. Bob manually shares his user event blob back to Alice (simulating network delivery)
    5. Alice receives and projects Bob's user event, validating the invite proof
    """

    # Setup database
    conn = sqlite3.connect(':memory:')
    db = Database(conn)
    schema.create_all(db)

    t_ms = int(time.time() * 1000)

    # === ALICE CREATES NETWORK ===

    # 1. Alice creates local peer
    alice_peer_id, alice_peer_shared_id = peer.create(t_ms, db)
    print(f"Alice peer_id: {alice_peer_id}")
    print(f"Alice peer_shared_id: {alice_peer_shared_id}")

    # 2. Alice creates network key
    network_key_id = key.create(alice_peer_id, t_ms, db)

    # 3. Alice creates network group
    alice_group_id = group.create(
        name="Default Group",
        peer_id=alice_peer_id,
        peer_shared_id=alice_peer_shared_id,
        key_id=network_key_id,
        t_ms=t_ms,
        db=db
    )
    print(f"Alice group_id: {alice_group_id}")

    # === ALICE CREATES INVITE ===

    # 4. Alice creates invite
    alice_invite_id, invite_link, invite_data = invite.create(
        inviter_peer_id=alice_peer_id,
        inviter_peer_shared_id=alice_peer_shared_id,
        group_id=alice_group_id,
        key_id=network_key_id,
        t_ms=t_ms,
        db=db
    )
    print(f"Invite ID: {alice_invite_id}")
    print(f"Invite link: {invite_link}")

    # Verify invite was projected
    invite_row = db.query_one(
        "SELECT * FROM invites WHERE invite_id = ?",
        (alice_invite_id,)
    )
    assert invite_row is not None, "Invite not found in invites table"
    print(f"Invite pubkey: {invite_row['invite_pubkey']}")

    # Verify invite marked as valid
    valid_row = db.query_one(
        "SELECT * FROM valid_events WHERE event_id = ?",
        (alice_invite_id,)
    )
    assert valid_row is not None, "Invite not marked as valid"

    # === BOB DECODES INVITE LINK ===

    # 5. Bob decodes invite link
    assert invite_link.startswith("quiet://invite/"), "Invalid invite link format"
    invite_code = invite_link[15:]  # Remove "quiet://invite/" prefix

    # Decode base64-urlsafe (add padding if needed)
    padded = invite_code + '=' * (-len(invite_code) % 4)
    decoded_json = base64.urlsafe_b64decode(padded).decode()
    decoded_invite_data = json.loads(decoded_json)

    print(f"Decoded invite keys: {sorted(decoded_invite_data.keys())}")

    # Verify all required fields present
    required_fields = [
        'invite_blob', 'invite_secret',
        'invite_key_secret', 'transit_secret', 'transit_secret_id'
    ]
    for field in required_fields:
        assert field in decoded_invite_data, f"Missing field: {field}"

    # Extract secrets from link
    bob_invite_blob_b64 = decoded_invite_data['invite_blob']
    bob_invite_secret = decoded_invite_data['invite_secret']
    bob_invite_key_secret_hex = decoded_invite_data['invite_key_secret']
    bob_transit_secret_hex = decoded_invite_data['transit_secret']
    bob_transit_secret_id = decoded_invite_data['transit_secret_id']

    print(f"Bob extracted invite_secret: {bob_invite_secret}")
    print(f"Bob extracted invite_blob size: {len(crypto.b64decode(bob_invite_blob_b64))} bytes")

    # === BOB CREATES PEER AND USER ===

    # 6. Bob creates local peer
    bob_peer_id, bob_peer_shared_id = peer.create(t_ms + 1000, db)
    print(f"Bob peer_id: {bob_peer_id}")
    print(f"Bob peer_shared_id: {bob_peer_shared_id}")

    # 7. Bob stores invite_key_secret locally (from invite link)
    bob_invite_key_secret = bytes.fromhex(bob_invite_key_secret_hex)
    bob_key_id = key.create(bob_peer_id, t_ms + 1000, db)

    # 8. Bob stores invite blob → unwraps → projects → has invite event!
    invite_blob_bytes = crypto.b64decode(bob_invite_blob_b64)
    bob_invite_id = store_module.event(invite_blob_bytes, bob_peer_id, t_ms + 1000, db)
    print(f"Bob stored invite_id: {bob_invite_id}")

    # 9. Bob queries invite to get group_id and connection info
    bob_invite_row = db.query_one("SELECT * FROM invites WHERE invite_id = ?", (bob_invite_id,))
    assert bob_invite_row is not None, "Bob's invite not projected"
    bob_group_id = bob_invite_row['group_id']
    print(f"Bob got group_id from invite: {bob_group_id}")

    # 10. Bob creates user event with invite proof (dependency check will pass - invite exists)
    bob_user_id = user.create(
        peer_id=bob_peer_id,
        peer_shared_id=bob_peer_shared_id,
        group_id=bob_group_id,
        name="Bob",
        key_id=bob_key_id,
        t_ms=t_ms + 1000,
        db=db,
        invite_secret=bob_invite_secret
    )
    print(f"Bob user_id: {bob_user_id}")

    # === BOB SENDS EVENTS BACK TO ALICE (SIMULATED) ===

    # 11. Bob extracts his peer_shared event blob to send to Alice first
    bob_peer_shared_blob = store_module.get(bob_peer_shared_id, db)
    assert bob_peer_shared_blob is not None, "Bob's peer_shared event blob not found"
    print(f"Bob's peer_shared event blob size: {len(bob_peer_shared_blob)} bytes")

    # 12. Alice receives Bob's peer_shared event
    alice_received_peer_shared_id = store_module.event(bob_peer_shared_blob, alice_peer_id, t_ms + 1500, db)
    print(f"Alice received peer_shared_id: {alice_received_peer_shared_id}")
    assert alice_received_peer_shared_id == bob_peer_shared_id, "Peer shared ID mismatch"

    # 13. Bob extracts his user event blob to send to Alice
    bob_user_blob = store_module.get(bob_user_id, db)
    assert bob_user_blob is not None, "Bob's user event blob not found"
    print(f"Bob's user event blob size: {len(bob_user_blob)} bytes")

    # 14. Alice receives Bob's user event (simulated network delivery)
    # Alice processes the blob through first_seen which will project it
    alice_received_user_id = store_module.event(bob_user_blob, alice_peer_id, t_ms + 2000, db)
    print(f"Alice received user_id: {alice_received_user_id}")
    assert alice_received_user_id == bob_user_id, "User event ID mismatch"

    # === VERIFY BOB'S USER EVENT WAS PROJECTED IN ALICE'S DB ===

    # 15. Verify user event was projected
    user_row = db.query_one(
        "SELECT * FROM users WHERE user_id = ?",
        (bob_user_id,)
    )
    assert user_row is not None, "User not found in users table after Alice received it"
    assert user_row['peer_id'] == bob_peer_shared_id, "User peer_id mismatch"
    assert user_row['name'] == "Bob", "User name mismatch"
    print(f"User invite_pubkey: {user_row['invite_pubkey']}")

    # 16. Verify group membership was created
    member_row = db.query_one(
        "SELECT * FROM group_members WHERE user_id = ?",
        (bob_user_id,)
    )
    assert member_row is not None, "Group membership not found"
    assert member_row['group_id'] == bob_group_id, "Group membership group_id mismatch"
    print(f"Group membership created for group: {member_row['group_id']}")

    # 17. Verify user marked as valid
    user_valid_row = db.query_one(
        "SELECT * FROM valid_events WHERE event_id = ?",
        (bob_user_id,)
    )
    assert user_valid_row is not None, "User event not marked as valid"

    # === VERIFY INVITE PROOF MATCHES ===

    # 18. Verify invite_pubkey matches between invite and user
    assert user_row['invite_pubkey'] == invite_row['invite_pubkey'], \
        "Invite pubkey mismatch between invite and user"

    # 19. Verify invite proof is valid
    bob_public_key = peer_shared.get_public_key(bob_peer_shared_id, bob_peer_id, db)
    bob_public_key_hex = bob_public_key.hex()

    is_valid = crypto.verify_invite_proof(
        invite_secret=bob_invite_secret,
        public_key=bob_public_key_hex,
        group_id=bob_group_id,
        claimed_pubkey=user_row['invite_pubkey'],
        claimed_signature=user_row.get('invite_signature', '')  # Would need to extract from event
    )

    # Note: invite_signature is in the event plaintext, not in users table
    # Let's extract it from the event to verify
    user_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (crypto.b64decode(bob_user_id),))
    assert user_blob is not None, "User event blob not found"

    unwrapped, _ = crypto.unwrap(user_blob['blob'], db)
    assert unwrapped is not None, "Failed to unwrap user event"

    user_event_data = crypto.parse_json(unwrapped)
    assert 'invite_signature' in user_event_data, "invite_signature missing from user event"

    is_valid = crypto.verify_invite_proof(
        invite_secret=bob_invite_secret,
        public_key=bob_public_key_hex,
        group_id=bob_group_id,
        claimed_pubkey=user_event_data['invite_pubkey'],
        claimed_signature=user_event_data['invite_signature']
    )
    assert is_valid, "Invite proof validation failed"
    print("✓ Invite proof is valid!")

    # === TEST INVALID INVITE SECRET ===

    # 20. Verify invalid invite secret fails validation
    wrong_secret = "wrong_secret_123"
    is_invalid = crypto.verify_invite_proof(
        invite_secret=wrong_secret,
        public_key=bob_public_key_hex,
        group_id=bob_group_id,
        claimed_pubkey=user_event_data['invite_pubkey'],
        claimed_signature=user_event_data['invite_signature']
    )
    assert not is_invalid, "Invalid invite secret should fail validation"
    print("✓ Invalid invite secret correctly rejected")

    print("\n=== ALL TESTS PASSED ===")


if __name__ == '__main__':
    test_invite_roundtrip()
