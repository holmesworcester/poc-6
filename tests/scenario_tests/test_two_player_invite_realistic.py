"""Realistic two-player invite and join scenario test.

Uses only API-style function calls with realistic bootstrap process:
- Alice creates network and invite
- Bob decodes invite link and joins
- Bob sends bootstrap events to Alice (job pattern, simulating UDP retries)
- Alice processes incoming and validates Bob's user
- Once validated, sync protocol works
- Both peers exchange messages
"""
import sqlite3
import base64
import json
import time

from db import Database
import schema
import crypto
import store
from events import peer, key, group, channel, invite, user, message, sync


def test_two_player_invite_realistic():
    """Complete two-player invite scenario with realistic bootstrap."""

    # Setup database
    conn = sqlite3.connect(':memory:')
    db = Database(conn)
    schema.create_all(db)

    t_ms = int(time.time() * 1000)

    # === PHASE 1: ALICE BOOTSTRAPS NETWORK ===
    print("\n=== Phase 1: Alice Bootstraps Network ===")

    # Alice creates peer
    alice_peer_id, alice_peer_shared_id = peer.create(t_ms=t_ms + 1000, db=db)
    print(f"Alice peer_id: {alice_peer_id[:16]}...")

    # Alice creates network infrastructure
    alice_key_id = key.create(alice_peer_id, t_ms=t_ms + 2000, db=db)
    alice_group_id = group.create(
        name="Alice's Network",
        peer_id=alice_peer_id,
        peer_shared_id=alice_peer_shared_id,
        key_id=alice_key_id,
        t_ms=t_ms + 3000,
        db=db
    )
    alice_channel_id = channel.create(
        name="general",
        group_id=alice_group_id,
        peer_id=alice_peer_id,
        peer_shared_id=alice_peer_shared_id,
        key_id=alice_key_id,
        t_ms=t_ms + 4000,
        db=db
    )
    print(f"Alice group_id: {alice_group_id[:16]}...")
    print(f"Alice channel_id: {alice_channel_id[:16]}...")

    # Alice creates invite
    alice_invite_id, invite_link, invite_data = invite.create(
        inviter_peer_id=alice_peer_id,
        inviter_peer_shared_id=alice_peer_shared_id,
        group_id=alice_group_id,
        key_id=alice_key_id,
        t_ms=t_ms + 5000,
        db=db
    )
    print(f"Invite created: {invite_link[:50]}...")

    # Alice stores the invite key secret locally (she'll need it to decrypt Bob's bootstrap events)
    # IMPORTANT: Create a proper key EVENT so get_peer_id_for_key() can find the owner
    alice_invite_key_secret = bytes.fromhex(invite_data['invite_key_secret'])

    # Create key event for invite key (owned by Alice)
    invite_key_event_data = {
        'type': 'key',
        'key': crypto.b64encode(alice_invite_key_secret),
        'peer_id': alice_peer_id,
        'created_at': t_ms + 5500
    }
    invite_key_event_blob = json.dumps(invite_key_event_data).encode()

    # Use deterministic ID based on key secret
    alice_invite_key_id_bytes = crypto.hash(alice_invite_key_secret, size=16)
    alice_invite_key_id = crypto.b64encode(alice_invite_key_id_bytes)

    # Store the event blob
    db.execute(
        "INSERT OR IGNORE INTO store (id, blob, stored_at) VALUES (?, ?, ?)",
        (alice_invite_key_id_bytes, invite_key_event_blob, t_ms + 5500)
    )

    # Also store in keys table for unwrapping
    db.execute(
        "INSERT OR IGNORE INTO keys (key_id, key, created_at) VALUES (?, ?, ?)",
        (alice_invite_key_id, alice_invite_key_secret, t_ms + 5500)
    )

    db.commit()

    # === PHASE 2: BOB RECEIVES INVITE (OUT OF BAND) ===
    print("\n=== Phase 2: Bob Receives Invite ===")

    # Decode invite link
    invite_code = invite_link[15:]  # Remove "quiet://invite/"
    padded = invite_code + '=' * (-len(invite_code) % 4)
    decoded_json = base64.urlsafe_b64decode(padded).decode()
    decoded_invite_data = json.loads(decoded_json)

    print(f"Invite decoded, keys: {list(decoded_invite_data.keys())}")
    print(f"Invite address: {decoded_invite_data.get('ip')}:{decoded_invite_data.get('port')}")

    # === PHASE 3: BOB JOINS NETWORK ===
    print("\n=== Phase 3: Bob Joins Network ===")

    # Bob creates peer
    bob_peer_id, bob_peer_shared_id = peer.create(t_ms=t_ms + 6000, db=db)
    print(f"Bob peer_id: {bob_peer_id[:16]}...")

    # Bob stores invite blob and projects it
    bob_invite_blob_b64 = decoded_invite_data['invite_blob']
    bob_invite_private_key = crypto.b64decode(decoded_invite_data['invite_private_key'])

    invite_blob_bytes = crypto.b64decode(bob_invite_blob_b64)
    bob_invite_blob_id = store.blob(invite_blob_bytes, t_ms=t_ms + 8000, return_dupes=True, db=db)

    from events import first_seen
    bob_invite_fs_id = first_seen.create(bob_invite_blob_id, bob_peer_id, t_ms=t_ms + 8000, db=db, return_dupes=True)
    first_seen.project(bob_invite_fs_id, db)

    # Bob queries invite to get group_id
    invite_row = db.query_one("SELECT group_id FROM invites WHERE invite_id = ?", (bob_invite_blob_id,))
    bob_group_id = invite_row['group_id']
    print(f"Bob got group_id from invite: {bob_group_id[:16]}...")

    # Bob creates invite key locally (for encrypting bootstrap events)
    # IMPORTANT: Use deterministic ID so Alice can unwrap events
    invite_key_secret_hex = decoded_invite_data['invite_key_secret']
    invite_key_secret = bytes.fromhex(invite_key_secret_hex)
    bob_invite_key_id_bytes = crypto.hash(invite_key_secret, size=16)
    bob_invite_key_id = crypto.b64encode(bob_invite_key_id_bytes)

    # Store the invite key with deterministic ID (key_id should be base64 string, not bytes)
    db.execute(
        "INSERT OR IGNORE INTO keys (key_id, key, created_at) VALUES (?, ?, ?)",
        (bob_invite_key_id, invite_key_secret, t_ms + 7000)
    )
    db.commit()

    # Bob creates user event with invite proof, encrypted with INVITE KEY (not personal key)
    # This way Alice can decrypt it using the invite key from the invite link
    bob_user_id, bob_prekey_id = user.create(
        peer_id=bob_peer_id,
        peer_shared_id=bob_peer_shared_id,
        group_id=bob_group_id,
        name="Bob",
        key_id=bob_invite_key_id,  # Use invite key, not personal key!
        t_ms=t_ms + 9000,
        db=db,
        invite_private_key=bob_invite_private_key
    )
    print(f"Bob user_id: {bob_user_id[:16]}..., prekey_id: {bob_prekey_id[:16]}...")

    db.commit()

    # === PHASE 4: BOB SENDS BOOTSTRAP EVENTS (JOB PATTERN) ===
    print("\n=== Phase 4: Bob Sends Bootstrap Events ===")

    # Bob repeatedly sends bootstrap events (simulating UDP retries)
    for attempt in range(3):
        print(f"Bootstrap attempt {attempt + 1}/3...")
        user.send_bootstrap_events(
            peer_id=bob_peer_id,
            peer_shared_id=bob_peer_shared_id,
            user_id=bob_user_id,
            prekey_id=bob_prekey_id,
            invite_data=decoded_invite_data,
            t_ms=t_ms + 10000 + (attempt * 1000),
            db=db
        )

    db.commit()

    # === PHASE 5: ALICE PROCESSES INCOMING (RECEIVES BOB'S BOOTSTRAP) ===
    print("\n=== Phase 5: Alice Processes Incoming ===")

    # Alice processes incoming queue
    sync.receive(batch_size=10, t_ms=t_ms + 15000, db=db)

    # Check blocked events
    blocked_events = db.query("SELECT * FROM blocked_events")
    print(f"Blocked events: {len(blocked_events)}")
    for be in blocked_events[:3]:  # Show first 3
        print(f"  Blocked: {be['first_seen_id'][:16]}... peer={be['seen_by_peer_id'][:16]}... deps={be['missing_deps'][:100]}")

    # Check if Alice has validated Bob's user
    bob_user_valid_for_alice = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND seen_by_peer_id = ?",
        (bob_user_id, alice_peer_id)
    )

    if bob_user_valid_for_alice:
        print("✓ Alice validated Bob's user event!")
    else:
        print("✗ Alice did NOT validate Bob's user event")

    # Check if Bob appears in Alice's users table
    bob_in_users = db.query_one(
        "SELECT * FROM users WHERE user_id = ?",
        (bob_user_id,)
    )
    if bob_in_users:
        print(f"✓ Bob found in users table: {bob_in_users['name']}")

    # Check if Bob is a group member
    bob_membership = db.query_one(
        "SELECT * FROM group_members WHERE user_id = ? AND group_id = ?",
        (bob_user_id, alice_group_id)
    )
    if bob_membership:
        print(f"✓ Bob is a member of Alice's group")

    # === PHASE 6: REGISTER ALICE'S PREKEY FOR SYNC ===
    print("\n=== Phase 6: Register Alice's Prekey for Sync ===")

    # Bob's prekey is already created by user.create()
    # Alice needs a prekey too (TODO: Alice should create a user event for herself)
    alice_public_key = peer.get_public_key(alice_peer_id, db)

    db.execute(
        "INSERT INTO pre_keys (peer_id, public_key, created_at) VALUES (?, ?, ?)",
        (alice_peer_id, alice_public_key, t_ms + 16000)
    )
    db.commit()

    # === PHASE 7: EXCHANGE EVENTS VIA SYNC PROTOCOL ===
    print("\n=== Phase 7: Exchange Events via Sync ===")

    # Bob sends sync request to Alice
    sync.send_request(
        to_peer_id=alice_peer_id,
        from_peer_id=bob_peer_id,
        from_peer_shared_id=bob_peer_shared_id,
        t_ms=t_ms + 18000,
        db=db
    )

    # Alice sends sync request to Bob
    sync.send_request(
        to_peer_id=bob_peer_id,
        from_peer_id=alice_peer_id,
        from_peer_shared_id=alice_peer_shared_id,
        t_ms=t_ms + 19000,
        db=db
    )

    # Process sync requests (unwraps requests, auto-sends responses)
    sync.receive(batch_size=10, t_ms=t_ms + 20000, db=db)

    # Process sync responses (receives actual events)
    sync.receive(batch_size=100, t_ms=t_ms + 21000, db=db)

    # Additional round for convergence
    sync.receive(batch_size=100, t_ms=t_ms + 22000, db=db)

    # === PHASE 8: MESSAGE EXCHANGE ===
    print("\n=== Phase 8: Message Exchange ===")

    # Alice sends message
    alice_msg = message.create_message(
        params={
            'content': 'Hello Bob!',
            'channel_id': alice_channel_id,
            'group_id': alice_group_id,
            'peer_id': alice_peer_id,
            'peer_shared_id': alice_peer_shared_id,
            'key_id': alice_key_id
        },
        t_ms=t_ms + 23000,
        db=db
    )
    print(f"Alice sent message: {alice_msg['id'][:16]}...")

    # Bob sends message
    bob_msg = message.create_message(
        params={
            'content': 'Hello Alice!',
            'channel_id': alice_channel_id,  # Same channel!
            'group_id': alice_group_id,      # Same group!
            'peer_id': bob_peer_id,
            'peer_shared_id': bob_peer_shared_id,
            'key_id': alice_key_id           # Uses Alice's network key
        },
        t_ms=t_ms + 24000,
        db=db
    )
    print(f"Bob sent message: {bob_msg['id'][:16]}...")

    # Exchange messages via sync
    sync.send_request(
        to_peer_id=bob_peer_id,
        from_peer_id=alice_peer_id,
        from_peer_shared_id=alice_peer_shared_id,
        t_ms=t_ms + 25000,
        db=db
    )
    sync.send_request(
        to_peer_id=alice_peer_id,
        from_peer_id=bob_peer_id,
        from_peer_shared_id=bob_peer_shared_id,
        t_ms=t_ms + 26000,
        db=db
    )
    sync.receive(batch_size=10, t_ms=t_ms + 27000, db=db)
    sync.receive(batch_size=100, t_ms=t_ms + 28000, db=db)

    # === PHASE 9: VERIFICATION ===
    print("\n=== Phase 9: Verification ===")

    # Verify Alice sees Bob's message
    alice_sees_bob_msg = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND seen_by_peer_id = ?",
        (bob_msg['id'], alice_peer_id)
    )
    assert alice_sees_bob_msg, "Alice should see Bob's message"
    print("✓ Alice sees Bob's message")

    # Verify Bob sees Alice's message
    bob_sees_alice_msg = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND seen_by_peer_id = ?",
        (alice_msg['id'], bob_peer_id)
    )
    assert bob_sees_alice_msg, "Bob should see Alice's message"
    print("✓ Bob sees Alice's message")

    # Verify Bob's group membership
    bob_member = db.query_one(
        "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?",
        (alice_group_id, bob_user_id)
    )
    assert bob_member, "Bob should be a member of Alice's group"
    print("✓ Bob is a member of Alice's group")

    # Verify invite proof
    bob_user_row = db.query_one("SELECT * FROM users WHERE user_id = ?", (bob_user_id,))
    invite_row_check = db.query_one("SELECT * FROM invites WHERE invite_id = ?", (alice_invite_id,))
    assert bob_user_row['invite_pubkey'] == invite_row_check['invite_pubkey'], "Invite pubkey should match"
    print("✓ Invite proof validated")

    print("\n=== ✓ ALL TESTS PASSED ===")


if __name__ == '__main__':
    test_two_player_invite_realistic()
