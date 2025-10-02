"""Test three-player messaging with realistic invite flow.

Alice creates a network and invites Bob. Charlie stays isolated in his own network.
Based on test_invite_roundtrip and test_multi_identity_chat_sim patterns.
"""

import sqlite3
import time

from db import Database
import schema
import crypto
import store
from events import peer, peer_shared, key, group, channel, invite, user, message, sync, prekey, first_seen


def test_three_player_invite_messaging():
    """Test messaging between Alice (inviter), Bob (invitee), and Charlie (isolated)."""

    # Setup database
    conn = sqlite3.connect(':memory:')
    db = Database(conn)
    schema.create_all(db)

    t_ms = int(time.time() * 1000)

    # === ALICE CREATES NETWORK ===
    print("\n=== Alice creates network ===")

    # Alice creates local peer
    alice_peer_id, alice_peer_shared_id = peer.create(t_ms, db)
    print(f"Alice peer_id: {alice_peer_id[:16]}...")
    print(f"Alice peer_shared_id: {alice_peer_shared_id[:16]}...")

    # Alice creates network key
    alice_network_key_id = key.create(alice_peer_id, t_ms + 100, db)

    # Alice creates network group
    alice_group_id = group.create(
        name="Alice's Group",
        peer_id=alice_peer_id,
        peer_shared_id=alice_peer_shared_id,
        key_id=alice_network_key_id,
        t_ms=t_ms + 200,
        db=db
    )

    # Alice creates default channel
    alice_channel_id = channel.create(
        name="general",
        group_id=alice_group_id,
        peer_id=alice_peer_id,
        peer_shared_id=alice_peer_shared_id,
        key_id=alice_network_key_id,
        t_ms=t_ms + 300,
        db=db
    )

    # Alice creates prekey for receiving sync requests
    alice_public_key = peer.get_public_key(alice_peer_id, db)

    # === ALICE CREATES INVITE FOR BOB ===
    print("\n=== Alice creates invite ===")

    alice_invite_id, invite_link, invite_data = invite.create(
        inviter_peer_id=alice_peer_id,
        inviter_peer_shared_id=alice_peer_shared_id,
        group_id=alice_group_id,
        key_id=alice_network_key_id,
        t_ms=t_ms + 400,
        db=db
    )
    print(f"Invite ID: {alice_invite_id[:16]}...")
    print(f"Invite link: {invite_link[:50]}...")

    # === BOB JOINS VIA INVITE ===
    print("\n=== Bob joins via invite ===")

    # Bob decodes invite link (simulated)
    import base64
    import json
    invite_code = invite_link[15:]  # Remove "quiet://invite/" prefix
    padded = invite_code + '=' * (-len(invite_code) % 4)
    decoded_json = base64.urlsafe_b64decode(padded).decode()
    decoded_invite_data = json.loads(decoded_json)

    # Bob extracts invite data
    bob_invite_secret = decoded_invite_data['invite_secret']

    # Bob creates local peer
    bob_peer_id, bob_peer_shared_id = peer.create(t_ms + 500, db)
    print(f"Bob peer_id: {bob_peer_id[:16]}...")
    print(f"Bob peer_shared_id: {bob_peer_shared_id[:16]}...")

    # Bob creates his own key (for local encryption)
    bob_key_id = key.create(bob_peer_id, t_ms + 600, db)

    # Bob stores invite blob first to get group_id
    bob_invite_blob_b64 = decoded_invite_data['invite_blob']
    bob_invite_blob_bytes = crypto.b64decode(bob_invite_blob_b64)
    bob_invite_id = store.event(bob_invite_blob_bytes, bob_peer_id, t_ms + 650, db)
    bob_invite_row = db.query_one("SELECT group_id FROM invites WHERE invite_id = ?", (bob_invite_id,))
    bob_group_id = bob_invite_row['group_id']

    # Bob creates user event with invite proof
    bob_user_id, bob_prekey_id = user.create(
        peer_id=bob_peer_id,
        peer_shared_id=bob_peer_shared_id,
        group_id=bob_group_id,
        name="Bob",
        key_id=bob_key_id,
        t_ms=t_ms + 700,
        db=db,
        invite_private_key=bob_invite_secret
    )
    print(f"Bob user_id: {bob_user_id[:16]}..., prekey_id: {bob_prekey_id[:16]}...")

    # Bob's public key for sync
    bob_public_key = peer.get_public_key(bob_peer_id, db)

    # === BOB SENDS HIS EVENTS TO ALICE (simulated network delivery) ===
    print("\n=== Bob shares events with Alice ===")

    # Bob shares his peer_shared event with Alice
    bob_peer_shared_blob = store.get(bob_peer_shared_id, db)
    alice_received_bob_ps = store.event(bob_peer_shared_blob, alice_peer_id, t_ms + 800, db)
    print(f"Alice received Bob's peer_shared: {alice_received_bob_ps[:16]}...")

    # Bob shares his user event with Alice
    bob_user_blob = store.get(bob_user_id, db)
    alice_received_bob_user = store.event(bob_user_blob, alice_peer_id, t_ms + 900, db)
    print(f"Alice received Bob's user: {alice_received_bob_user[:16]}...")

    # === ALICE SHARES HER EVENTS WITH BOB ===
    print("\n=== Alice shares events with Bob ===")

    # Alice shares her peer_shared with Bob
    alice_peer_shared_blob = store.get(alice_peer_shared_id, db)
    bob_received_alice_ps = store.event(alice_peer_shared_blob, bob_peer_id, t_ms + 1000, db)

    # Alice shares her network key with Bob (so he can decrypt messages)
    alice_key_blob = store.get(alice_network_key_id, db)
    bob_received_alice_key = store.event(alice_key_blob, bob_peer_id, t_ms + 1100, db)

    # Alice shares group/channel with Bob
    alice_group_blob = store.get(alice_group_id, db)
    bob_received_alice_group = store.event(alice_group_blob, bob_peer_id, t_ms + 1200, db)

    alice_channel_blob = store.get(alice_channel_id, db)
    bob_received_alice_channel = store.event(alice_channel_blob, bob_peer_id, t_ms + 1300, db)

    # Register prekeys for Alice and Bob to sync
    db.execute(
        "INSERT INTO pre_keys (peer_id, public_key, created_at) VALUES (?, ?, ?)",
        (alice_peer_id, alice_public_key, t_ms + 1400)
    )
    db.execute(
        "INSERT INTO pre_keys (peer_id, public_key, created_at) VALUES (?, ?, ?)",
        (bob_peer_id, bob_public_key, t_ms + 1500)
    )

    # === CHARLIE CREATES ISOLATED NETWORK ===
    print("\n=== Charlie creates isolated network ===")

    # Charlie creates local peer
    charlie_peer_id, charlie_peer_shared_id = peer.create(t_ms + 2000, db)
    print(f"Charlie peer_id: {charlie_peer_id[:16]}...")
    print(f"Charlie peer_shared_id: {charlie_peer_shared_id[:16]}...")

    # Charlie creates his own network (no connection to Alice/Bob)
    charlie_key_id = key.create(charlie_peer_id, t_ms + 2100, db)
    charlie_group_id = group.create(
        name="Charlie's Group",
        peer_id=charlie_peer_id,
        peer_shared_id=charlie_peer_shared_id,
        key_id=charlie_key_id,
        t_ms=t_ms + 2200,
        db=db
    )
    charlie_channel_id = channel.create(
        name="general",
        group_id=charlie_group_id,
        peer_id=charlie_peer_id,
        peer_shared_id=charlie_peer_shared_id,
        key_id=charlie_key_id,
        t_ms=t_ms + 2300,
        db=db
    )

    # Charlie has a prekey but Alice/Bob don't know about it
    charlie_public_key = peer.get_public_key(charlie_peer_id, db)

    # === ALL THREE CREATE MESSAGES ===
    print("\n=== Creating messages ===")

    # Alice sends message in her network
    alice_msg = message.create_message(
        params={
            'content': 'Hello from Alice!',
            'channel_id': alice_channel_id,
            'group_id': alice_group_id,
            'peer_id': alice_peer_id,
            'peer_shared_id': alice_peer_shared_id,
            'key_id': alice_network_key_id
        },
        t_ms=t_ms + 3000,
        db=db
    )
    print(f"Alice message ID: {alice_msg['id']}")

    # Bob sends message in Alice's network (he joined via invite)
    bob_msg = message.create_message(
        params={
            'content': 'Hello from Bob!',
            'channel_id': alice_channel_id,  # Same channel as Alice
            'group_id': alice_group_id,       # Same group as Alice
            'peer_id': bob_peer_id,
            'peer_shared_id': bob_peer_shared_id,
            'key_id': alice_network_key_id   # Uses Alice's network key
        },
        t_ms=t_ms + 3100,
        db=db
    )
    print(f"Bob message ID: {bob_msg['id']}")

    # Charlie sends message in his own network
    charlie_msg = message.create_message(
        params={
            'content': 'Hello from Charlie!',
            'channel_id': charlie_channel_id,
            'group_id': charlie_group_id,
            'peer_id': charlie_peer_id,
            'peer_shared_id': charlie_peer_shared_id,
            'key_id': charlie_key_id
        },
        t_ms=t_ms + 3200,
        db=db
    )
    print(f"Charlie message ID: {charlie_msg['id']}")

    db.commit()

    # === SYNC REQUESTS ===
    print("\n=== Sending sync requests ===")

    # Alice and Bob send sync requests to each other
    sync.send_request(
        to_peer_id=bob_peer_id,
        from_peer_id=alice_peer_id,
        from_peer_shared_id=alice_peer_shared_id,
        t_ms=t_ms + 4000,
        db=db
    )
    sync.send_request(
        to_peer_id=alice_peer_id,
        from_peer_id=bob_peer_id,
        from_peer_shared_id=bob_peer_shared_id,
        t_ms=t_ms + 4100,
        db=db
    )

    # Charlie tries to send sync requests but they'll fail (no prekeys)
    try:
        sync.send_request(
            to_peer_id=alice_peer_id,
            from_peer_id=charlie_peer_id,
            from_peer_shared_id=charlie_peer_shared_id,
            t_ms=t_ms + 4200,
            db=db
        )
    except:
        print("Charlie->Alice sync request failed (expected - no prekey)")

    # === PROCESS SYNC ===
    print("\n=== Processing sync ===")

    # Round 1: Process sync requests (triggers auto-responses)
    print("Round 1: Processing sync requests...")
    sync.receive(batch_size=10, t_ms=t_ms + 5000, db=db)

    # Round 2: Process sync responses
    print("Round 2: Processing sync responses...")
    sync.receive(batch_size=100, t_ms=t_ms + 6000, db=db)

    # Round 3: Process any remaining events (for blocked event resolution)
    print("Round 3: Additional sync processing...")
    sync.receive(batch_size=100, t_ms=t_ms + 7000, db=db)

    # === VERIFY VISIBILITY ===
    print("\n=== Verifying message visibility ===")

    # Check shareable events
    shareable = db.query("SELECT * FROM shareable_events ORDER BY peer_id")
    print(f"\nShareable events: {len(shareable)}")
    peer_counts = {}
    for s in shareable:
        peer_id = s['peer_id']
        if peer_id not in peer_counts:
            peer_counts[peer_id] = []
        peer_counts[peer_id].append(s['event_id'])

    for peer_id, events in peer_counts.items():
        print(f"  Peer {peer_id[:16]}... has {len(events)} shareable events")
        # Check if this includes messages
        for event_id in events:
            if event_id in [alice_msg['id'], bob_msg['id'], charlie_msg['id']]:
                print(f"    -> Includes message {event_id[:16]}...")

    # Check blocked events
    blocked = db.query("SELECT * FROM blocked_events")
    print(f"\nBlocked events: {len(blocked)}")
    for b in blocked:
        print(f"  {b['first_seen_id'][:16]}... peer={b['seen_by_peer_id'][:16]}... deps={b['missing_deps'][:50]}...")

    # Check messages table
    messages_in_db = db.query("SELECT * FROM messages")
    print(f"\nMessages in database: {len(messages_in_db)}")
    for m in messages_in_db:
        print(f"  Message {m['message_id'][:16]}... by {m['author_id'][:16]}... for peer {m['seen_by_peer_id'][:16]}...")

    # Check which messages are marked as valid
    valid_messages = db.query("""
        SELECT event_id, seen_by_peer_id
        FROM valid_events
        WHERE event_id IN (?, ?, ?)
    """, (alice_msg['id'], bob_msg['id'], charlie_msg['id']))

    print(f"\nValid message events: {len(valid_messages)}")
    for vm in valid_messages:
        print(f"  {vm['event_id'][:16]}... valid for {vm['seen_by_peer_id'][:16]}...")

    # Check valid events for each peer
    alice_valid = db.query(
        "SELECT COUNT(*) as count FROM valid_events WHERE seen_by_peer_id = ?",
        (alice_peer_id,)
    )[0]['count']
    bob_valid = db.query(
        "SELECT COUNT(*) as count FROM valid_events WHERE seen_by_peer_id = ?",
        (bob_peer_id,)
    )[0]['count']
    charlie_valid = db.query(
        "SELECT COUNT(*) as count FROM valid_events WHERE seen_by_peer_id = ?",
        (charlie_peer_id,)
    )[0]['count']

    print(f"Alice has {alice_valid} valid events")
    print(f"Bob has {bob_valid} valid events")
    print(f"Charlie has {charlie_valid} valid events")

    # Check specific message visibility
    print(f"\nChecking visibility for Alice (peer_id={alice_peer_id[:16]}...):")
    print(f"  Looking for Bob's message {bob_msg['id'][:16]}...")
    alice_sees_bob = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND seen_by_peer_id = ?",
        (bob_msg['id'], alice_peer_id)
    )
    print(f"  Result: {alice_sees_bob}")

    print(f"\nChecking visibility for Bob (peer_id={bob_peer_id[:16]}...):")
    print(f"  Looking for Alice's message {alice_msg['id'][:16]}...")
    bob_sees_alice = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND seen_by_peer_id = ?",
        (alice_msg['id'], bob_peer_id)
    )
    print(f"  Result: {bob_sees_alice}")
    charlie_sees_alice = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND seen_by_peer_id = ?",
        (alice_msg['id'], charlie_peer_id)
    )
    charlie_sees_bob = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND seen_by_peer_id = ?",
        (bob_msg['id'], charlie_peer_id)
    )
    alice_sees_charlie = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND seen_by_peer_id = ?",
        (charlie_msg['id'], alice_peer_id)
    )
    bob_sees_charlie = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND seen_by_peer_id = ?",
        (charlie_msg['id'], bob_peer_id)
    )

    print(f"\nAlice sees Bob's message: {alice_sees_bob is not None}")
    print(f"Bob sees Alice's message: {bob_sees_alice is not None}")
    print(f"Charlie sees Alice's message: {charlie_sees_alice is not None}")
    print(f"Charlie sees Bob's message: {charlie_sees_bob is not None}")
    print(f"Alice sees Charlie's message: {alice_sees_charlie is not None}")
    print(f"Bob sees Charlie's message: {bob_sees_charlie is not None}")

    # Assertions
    assert alice_sees_bob, "Alice should see Bob's message"
    assert bob_sees_alice, "Bob should see Alice's message"
    assert not charlie_sees_alice, "Charlie should NOT see Alice's message"
    assert not charlie_sees_bob, "Charlie should NOT see Bob's message"
    assert not alice_sees_charlie, "Alice should NOT see Charlie's message"
    assert not bob_sees_charlie, "Bob should NOT see Charlie's message"

    print("\n✓ All assertions passed!")


if __name__ == '__main__':
    test_three_player_invite_messaging()