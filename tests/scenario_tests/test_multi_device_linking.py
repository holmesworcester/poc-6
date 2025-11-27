"""
Scenario test: Multi-device linking.

Alice creates a network on her phone, then links her laptop to the same user account.
Both devices can send/receive messages and see the same conversation.

Tests:
- Alice creates network on phone
- Alice links laptop to her account via link URL
- Both devices have same user_id but different peer_ids
- Laptop receives group keys via GKS after linking
- Both devices can send and receive messages
- Network sees both devices as belonging to same user
"""
import sqlite3
from db import Database
import schema
from events.identity import user, invite, peer_shared, peer
from events.content import message
from tests.utils import tick_helper


def test_alice_links_phone_to_laptop(fresh_db):
    """Alice links her phone and laptop via link URL."""

    # Setup
    db = fresh_db

    print("\n=== Setup: Alice creates network on phone ===")

    # Alice creates a network on her phone
    alice_phone = user.new_network(name='Alice', t_ms=1000, db=db, device_name='Phone')
    print(f"Alice created network on phone")
    print(f"  peer_id={alice_phone['peer_id'][:20]}...")
    print(f"  user_id={alice_phone['user_id'][:20]}...")
    print(f"  channel_id={alice_phone['channel_id'][:20]}...")

    db.commit()

    # Alice creates a peer invite on her phone for device linking
    print("\n=== Alice creates peer invite for laptop (device linking) ===")

    invite_id, link_url, invite_data = invite.create(
        peer_id=alice_phone['peer_id'],
        t_ms=2000,
        db=db,
        mode='peer',
        user_id=alice_phone['user_id']
    )
    print(f"Peer invite created: {invite_id[:20]}...")
    print(f"Link URL: {link_url[:50]}...")

    db.commit()

    # Alice links laptop to her account via the link URL
    print("\n=== Alice links laptop to her account ===")

    # Create peer for laptop
    alice_laptop_peer_id = peer.create(t_ms=3000, db=db)
    print(f"Created laptop peer: {alice_laptop_peer_id[:20]}...")

    # Accept the invite (handles parsing, projection of backgrounds, and invite_accepted)
    accepted = invite.accept(alice_laptop_peer_id, link_url, t_ms=3001, db=db)
    assert accepted['mode'] == 'peer', f"Expected mode='peer', got {accepted['mode']}"
    print(f"Accepted peer invite from link URL")

    # Complete the peer linking via peer_shared.join()
    alice_laptop = peer_shared.join(
        peer_id=alice_laptop_peer_id,
        peer_invite_id=accepted['invite_id'],
        peer_invite_private_key=accepted['invite_private_key'],
        user_id=accepted['user_id'],
        prekey_id=accepted['invite_prekey_id'],
        t_ms=3002,
        db=db,
        device_name='Laptop'
    )
    print(f"Alice linked laptop")
    print(f"  peer_id={alice_laptop['peer_id'][:20]}...")
    print(f"  user_id={alice_laptop['user_id'][:20]}...")

    db.commit()

    # Verify both devices have the same user_id
    assert alice_laptop['user_id'] == alice_phone['user_id'], \
        f"Both devices should have same user_id"
    print(f"✅ Both devices share user_id: {alice_laptop['user_id'][:20]}...")

    # Verify devices have different peer_ids
    assert alice_laptop['peer_id'] != alice_phone['peer_id'], \
        f"Devices should have different peer_ids"
    print(f"✅ Devices have different peer_ids")

    # Verify device names are stored correctly
    phone_device_name = peer_shared.get_device_name(alice_phone['peer_shared_id'], alice_phone['peer_id'], db)
    laptop_device_name = peer_shared.get_device_name(alice_laptop['peer_shared_id'], alice_laptop['peer_id'], db)
    assert phone_device_name == 'Phone', f"Phone device name should be 'Phone', got '{phone_device_name}'"
    assert laptop_device_name == 'Laptop', f"Laptop device name should be 'Laptop', got '{laptop_device_name}'"
    print(f"✅ Device names stored correctly: Phone='{phone_device_name}', Laptop='{laptop_device_name}'")

    # Initial sync to converge (multiple rounds for GKS propagation)
    print("\n=== Initial sync to propagate link event and group keys ===")

    tick_helper.sync_until_converged(db=db, start_t_ms=4000, max_rounds=200, check_interval=1)

    # Verify laptop has the group key (from GKS)
    laptop_has_key = db.query_one(
        "SELECT 1 FROM group_keys WHERE key_id = ? AND recorded_by = ?",
        (alice_phone['key_id'], alice_laptop['peer_id'])
    )
    print(f"Laptop has group key: {bool(laptop_has_key)}")

    assert laptop_has_key, \
        f"Laptop should have Alice's group key after sync (received via group_key_shared)"
    print(f"✅ Laptop received group key via GKS")

    # Verify laptop has valid channel
    laptop_channel_valid = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (alice_phone['channel_id'], alice_laptop['peer_id'])
    )
    print(f"Laptop has valid channel: {bool(laptop_channel_valid)}")

    assert laptop_channel_valid, \
        f"Laptop should have valid channel after bootstrap"
    print(f"✅ Laptop has valid channel")

    # Create messages on both devices
    print("\n=== Creating messages on both devices ===")

    # Alice sends from phone
    alice_phone_msg = message.create(
        peer_id=alice_phone['peer_id'],
        channel_id=alice_phone['channel_id'],
        content="Hello from Alice's phone!",
        t_ms=5000,
        db=db
    )
    db.commit()
    print(f"Alice (phone) created message: {alice_phone_msg['id'][:20]}...")

    # Alice sends from laptop (uses same channel as phone since same user)
    alice_laptop_msg = message.create(
        peer_id=alice_laptop['peer_id'],
        channel_id=alice_phone['channel_id'],  # Same user, same channel
        content="Hello from Alice's laptop!",
        t_ms=5100,
        db=db
    )
    db.commit()
    print(f"Alice (laptop) created message: {alice_laptop_msg['id'][:20]}...")

    # Sync messages between devices
    print("\n=== Sync messages between devices ===")

    tick_helper.sync_until_converged(db=db, start_t_ms=6000, max_rounds=200, check_interval=1)

    # Verify both devices see both messages
    print("\n=== Verifying message delivery ===")

    # Discover channels for both devices (they should have the same channel)
    phone_channels = db.query_all(
        "SELECT DISTINCT channel_id FROM channels WHERE recorded_by = ?",
        (alice_phone['peer_id'],)
    )
    laptop_channels = db.query_all(
        "SELECT DISTINCT channel_id FROM channels WHERE recorded_by = ?",
        (alice_laptop['peer_id'],)
    )

    assert len(phone_channels) == 1, f"Phone should have 1 channel, got {len(phone_channels)}"
    assert len(laptop_channels) == 1, f"Laptop should have 1 channel, got {len(laptop_channels)}"

    phone_channel_id = phone_channels[0]['channel_id']
    laptop_channel_id = laptop_channels[0]['channel_id']

    assert phone_channel_id == laptop_channel_id, \
        "Phone and laptop should share the same channel"
    print(f"✅ Both devices share channel: {phone_channel_id[:20]}...")

    phone_messages = message.list(phone_channel_id, alice_phone['peer_id'], db)
    laptop_messages = message.list(laptop_channel_id, alice_laptop['peer_id'], db)

    phone_contents = [msg['content'] for msg in phone_messages]
    laptop_contents = [msg['content'] for msg in laptop_messages]

    print(f"Phone sees {len(phone_messages)} messages: {phone_contents}")
    print(f"Laptop sees {len(laptop_messages)} messages: {laptop_contents}")

    # Phone should see both messages
    assert "Hello from Alice's phone!" in phone_contents, \
        "Phone should see its own message"
    assert "Hello from Alice's laptop!" in phone_contents, \
        "Phone should see laptop's message"

    # Laptop should see both messages
    assert "Hello from Alice's phone!" in laptop_contents, \
        "Laptop should see phone's message"
    assert "Hello from Alice's laptop!" in laptop_contents, \
        "Laptop should see its own message"

    print(f"\n✅ All assertions passed!")


def test_alice_laptop_joins_after_phone_has_messages(fresh_db):
    """Alice's laptop joins after phone already has messages."""

    # Setup
    db = fresh_db

    print("\n=== Setup: Alice creates network and sends messages ===")

    # Alice creates network on phone
    alice_phone = user.new_network(name='Alice', t_ms=1000, db=db, device_name='Phone')
    db.commit()

    # Alice sends messages from phone first
    msg1 = message.create(
        peer_id=alice_phone['peer_id'],
        channel_id=alice_phone['channel_id'],
        content="Message 1 from phone",
        t_ms=1500,
        db=db
    )
    db.commit()

    msg2 = message.create(
        peer_id=alice_phone['peer_id'],
        channel_id=alice_phone['channel_id'],
        content="Message 2 from phone",
        t_ms=1600,
        db=db
    )
    db.commit()

    print(f"Phone sent 2 messages")

    # Now link laptop
    print("\n=== Link laptop after messages already exist ===")

    invite_id, link_url, invite_data = invite.create(
        peer_id=alice_phone['peer_id'],
        t_ms=2000,
        db=db,
        mode='peer',
        user_id=alice_phone['user_id']
    )
    db.commit()

    alice_laptop_peer_id = peer.create(t_ms=3000, db=db)
    accepted = invite.accept(alice_laptop_peer_id, link_url, t_ms=3001, db=db)
    assert accepted['mode'] == 'peer'
    alice_laptop = peer_shared.join(
        peer_id=alice_laptop_peer_id,
        peer_invite_id=accepted['invite_id'],
        peer_invite_private_key=accepted['invite_private_key'],
        user_id=accepted['user_id'],
        prekey_id=accepted['invite_prekey_id'],
        t_ms=3002,
        db=db,
        device_name='Laptop'
    )
    db.commit()

    # Verify device names are stored
    laptop_device_name = peer_shared.get_device_name(alice_laptop['peer_shared_id'], alice_laptop['peer_id'], db)
    assert laptop_device_name == 'Laptop', f"Device name should be 'Laptop', got '{laptop_device_name}'"

    # Sync to propagate messages to laptop
    print("\n=== Sync messages to laptop ===")

    tick_helper.sync_until_converged(db=db, start_t_ms=4000, max_rounds=200, check_interval=1)

    # Discover channel via sync (laptop should have same channel as phone through user linking)
    laptop_channels = db.query_all(
        "SELECT DISTINCT channel_id FROM channels WHERE recorded_by = ?",
        (alice_laptop['peer_id'],)
    )
    print(f"Laptop discovered {len(laptop_channels)} channel(s)")

    assert len(laptop_channels) == 1, \
        f"Laptop should have exactly one channel (shared with phone), got {len(laptop_channels)}"

    laptop_channel_id = laptop_channels[0]['channel_id']
    print(f"Laptop using channel: {laptop_channel_id[:20]}...")

    # Laptop should see all historical messages
    laptop_messages = message.list(
        laptop_channel_id,
        alice_laptop['peer_id'],
        db
    )
    laptop_contents = [msg['content'] for msg in laptop_messages]

    print(f"Laptop sees {len(laptop_messages)} messages: {laptop_contents}")

    assert len(laptop_messages) == 2, \
        f"Laptop should see both historical messages, got {len(laptop_messages)}"
    assert "Message 1 from phone" in laptop_contents
    assert "Message 2 from phone" in laptop_contents

    print(f"✅ Laptop received all historical messages after linking!")


def test_three_devices_all_linked(fresh_db):
    """Alice links phone, laptop, and tablet - all three share messages."""

    # Setup
    db = fresh_db

    print("\n=== Setup: Create network on phone ===")

    alice_phone = user.new_network(name='Alice', t_ms=1000, db=db, device_name='Phone')
    db.commit()

    # Link laptop
    print("\n=== Link laptop ===")

    invite_id_1, link_url_1, _ = invite.create(
        peer_id=alice_phone['peer_id'],
        t_ms=2000,
        db=db,
        mode='peer',
        user_id=alice_phone['user_id']
    )
    db.commit()

    alice_laptop_peer_id = peer.create(t_ms=3000, db=db)
    accepted_1 = invite.accept(alice_laptop_peer_id, link_url_1, t_ms=3001, db=db)
    alice_laptop = peer_shared.join(
        peer_id=alice_laptop_peer_id,
        peer_invite_id=accepted_1['invite_id'],
        peer_invite_private_key=accepted_1['invite_private_key'],
        user_id=accepted_1['user_id'],
        prekey_id=accepted_1['invite_prekey_id'],
        t_ms=3002,
        db=db,
        device_name='Laptop'
    )
    db.commit()

    # Link tablet
    print("\n=== Link tablet ===")

    invite_id_2, link_url_2, _ = invite.create(
        peer_id=alice_phone['peer_id'],  # Can create from phone or laptop
        t_ms=4000,
        db=db,
        mode='peer',
        user_id=alice_phone['user_id']
    )
    db.commit()

    alice_tablet_peer_id = peer.create(t_ms=5000, db=db)
    accepted_2 = invite.accept(alice_tablet_peer_id, link_url_2, t_ms=5001, db=db)
    alice_tablet = peer_shared.join(
        peer_id=alice_tablet_peer_id,
        peer_invite_id=accepted_2['invite_id'],
        peer_invite_private_key=accepted_2['invite_private_key'],
        user_id=accepted_2['user_id'],
        prekey_id=accepted_2['invite_prekey_id'],
        t_ms=5002,
        db=db,
        device_name='Tablet'
    )
    db.commit()

    # All three should have same user_id
    assert alice_laptop['user_id'] == alice_phone['user_id']
    assert alice_tablet['user_id'] == alice_phone['user_id']
    print(f"✅ All three devices share user_id: {alice_phone['user_id'][:20]}...")

    # Verify device names are stored correctly
    phone_device_name = peer_shared.get_device_name(alice_phone['peer_shared_id'], alice_phone['peer_id'], db)
    laptop_device_name = peer_shared.get_device_name(alice_laptop['peer_shared_id'], alice_laptop['peer_id'], db)
    tablet_device_name = peer_shared.get_device_name(alice_tablet['peer_shared_id'], alice_tablet['peer_id'], db)
    assert phone_device_name == 'Phone', f"Phone device name should be 'Phone', got '{phone_device_name}'"
    assert laptop_device_name == 'Laptop', f"Laptop device name should be 'Laptop', got '{laptop_device_name}'"
    assert tablet_device_name == 'Tablet', f"Tablet device name should be 'Tablet', got '{tablet_device_name}'"
    print(f"✅ All device names stored correctly: Phone, Laptop, Tablet")

    # Sync
    print("\n=== Sync all devices ===")

    tick_helper.sync_until_converged(db=db, start_t_ms=6000, max_rounds=200, check_interval=1)

    # Discover the shared channel for all devices
    print("\n=== Discovering shared channel ===")

    phone_channels = db.query_all(
        "SELECT DISTINCT channel_id FROM channels WHERE recorded_by = ?",
        (alice_phone['peer_id'],)
    )
    assert len(phone_channels) == 1, f"Phone should have 1 channel, got {len(phone_channels)}"
    shared_channel_id = phone_channels[0]['channel_id']
    print(f"All devices will use shared channel: {shared_channel_id[:20]}...")

    # Each device sends a message
    print("\n=== Each device sends a message ===")

    msg_phone = message.create(
        peer_id=alice_phone['peer_id'],
        channel_id=shared_channel_id,
        content="From phone",
        t_ms=7000,
        db=db
    )
    db.commit()

    msg_laptop = message.create(
        peer_id=alice_laptop['peer_id'],
        channel_id=shared_channel_id,
        content="From laptop",
        t_ms=7100,
        db=db
    )
    db.commit()

    msg_tablet = message.create(
        peer_id=alice_tablet['peer_id'],
        channel_id=shared_channel_id,
        content="From tablet",
        t_ms=7200,
        db=db
    )
    db.commit()

    # Sync messages
    print("\n=== Sync messages ===")

    tick_helper.sync_until_converged(db=db, start_t_ms=8000, max_rounds=200, check_interval=1)

    # Discover channels via sync for each device
    print("\n=== Discovering channels for each device ===")

    phone_channels = db.query_all(
        "SELECT DISTINCT channel_id FROM channels WHERE recorded_by = ?",
        (alice_phone['peer_id'],)
    )
    laptop_channels = db.query_all(
        "SELECT DISTINCT channel_id FROM channels WHERE recorded_by = ?",
        (alice_laptop['peer_id'],)
    )
    tablet_channels = db.query_all(
        "SELECT DISTINCT channel_id FROM channels WHERE recorded_by = ?",
        (alice_tablet['peer_id'],)
    )

    assert len(phone_channels) == 1, f"Phone should have 1 channel, got {len(phone_channels)}"
    assert len(laptop_channels) == 1, f"Laptop should have 1 channel, got {len(laptop_channels)}"
    assert len(tablet_channels) == 1, f"Tablet should have 1 channel, got {len(tablet_channels)}"

    phone_channel_id = phone_channels[0]['channel_id']
    laptop_channel_id = laptop_channels[0]['channel_id']
    tablet_channel_id = tablet_channels[0]['channel_id']

    # All should be the same channel (same user)
    assert phone_channel_id == laptop_channel_id == tablet_channel_id, \
        "All devices should share the same channel"
    print(f"✅ All devices share channel: {phone_channel_id[:20]}...")

    # All devices should see all three messages
    phone_msgs = message.list(phone_channel_id, alice_phone['peer_id'], db)
    laptop_msgs = message.list(laptop_channel_id, alice_laptop['peer_id'], db)
    tablet_msgs = message.list(tablet_channel_id, alice_tablet['peer_id'], db)

    print(f"Phone sees: {[m['content'] for m in phone_msgs]}")
    print(f"Laptop sees: {[m['content'] for m in laptop_msgs]}")
    print(f"Tablet sees: {[m['content'] for m in tablet_msgs]}")

    assert len(phone_msgs) == 3
    assert len(laptop_msgs) == 3
    assert len(tablet_msgs) == 3

    contents_phone = [m['content'] for m in phone_msgs]
    for device_msg in ["From phone", "From laptop", "From tablet"]:
        assert device_msg in contents_phone

    print(f"✅ All three devices see all messages!")
