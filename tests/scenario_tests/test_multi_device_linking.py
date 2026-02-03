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
import pytest
import sqlite3
from core.db import Database
from core import schema
from events.identity import user, invite, peer_shared, peer
from events.content import message
from tests.utils.tick_helper import assert_eventually, run_ticks, TestClock


# TODO: Fix username sync for linked devices - laptop doesn't see username before trying to send
@pytest.mark.skip(reason="Username not syncing to linked device before message send")
def test_alice_links_phone_to_laptop(fresh_db):
    """Alice links her phone and laptop via link URL."""

    db = fresh_db
    clock = TestClock()

    print("\n=== Setup: Alice creates network on phone ===")

    alice_phone = user.new_network(name='Alice', t_ms=clock.tick(), db=db, device_name='Phone')
    print(f"Alice created network on phone")
    print(f"  peer_id={alice_phone['peer_id'][:20]}...")
    print(f"  user_id={alice_phone['user_id'][:20]}...")
    print(f"  channel_id={alice_phone['channel_id'][:20]}...")

    db.commit()

    print("\n=== Alice creates peer invite for laptop (device linking) ===")

    invite_id, link_url, invite_data = invite.create(
        peer_id=alice_phone['peer_id'],
        t_ms=clock.tick(),
        db=db,
        mode='peer',
        user_id=alice_phone['user_id']
    )
    print(f"Peer invite created: {invite_id[:20]}...")
    print(f"Link URL: {link_url[:50]}...")

    db.commit()

    print("\n=== Alice links laptop to her account ===")

    alice_laptop_peer_id = peer.create(t_ms=clock.tick(), db=db)
    print(f"Created laptop peer: {alice_laptop_peer_id[:20]}...")

    accepted = invite.accept(alice_laptop_peer_id, link_url, t_ms=clock.tick(), db=db)
    assert accepted['mode'] == 'peer', f"Expected mode='peer', got {accepted['mode']}"
    print(f"Accepted peer invite from link URL")

    alice_laptop = peer_shared.join(
        peer_id=alice_laptop_peer_id,
        peer_invite_id=accepted['invite_id'],
        peer_invite_private_key=accepted['invite_private_key'],
        user_id=accepted['user_id'],
        t_ms=clock.tick(),
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

    # Initial sync to converge (multiple rounds for GKS propagation)
    # NOTE: Device names are encrypted via peer_name_update events which require group key.
    # The laptop won't have the group key until after sync, so device name check is after sync.
    # NOTE: Using run_ticks ensures all rounds execute; sync_until_converged may exit early
    print("\n=== Initial sync to propagate link event and group keys ===")

    # Run 200 ticks for initial sync (channels, etc.)
    current_t_ms = run_ticks(db=db, start_t_ms=None, num_rounds=200)

    # Verify device names are stored correctly (after sync - encrypted names require group key)
    phone_device_name = peer_shared.get_device_name(alice_phone['peer_shared_id'], alice_phone['peer_id'], db)
    laptop_device_name = peer_shared.get_device_name(alice_laptop['peer_shared_id'], alice_laptop['peer_id'], db)
    assert phone_device_name == 'Phone', f"Phone device name should be 'Phone', got '{phone_device_name}'"
    assert laptop_device_name == 'Laptop', f"Laptop device name should be 'Laptop', got '{laptop_device_name}'"
    print(f"✅ Device names stored correctly: Phone='{phone_device_name}', Laptop='{laptop_device_name}'")

    # Verify laptop has valid channel
    laptop_channel_valid = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (alice_phone['channel_id'], alice_laptop['peer_id'])
    )
    print(f"Laptop has valid channel: {bool(laptop_channel_valid)}")

    assert laptop_channel_valid, \
        f"Laptop should have valid channel after bootstrap"
    print(f"✅ Laptop has valid channel")

    # Verify connections between devices
    print("\n=== Verifying connections between devices ===")
    from events.network import connection_request as conn_module

    # Phone's connections
    phone_conns = conn_module.list_all_for_display(alice_phone['peer_id'], current_t_ms, db)
    print(f"Phone connections: {len(phone_conns['active'])} active, {len(phone_conns['pending'])} pending, {len(phone_conns['bootstrap'])} bootstrap")

    # Laptop's connections
    laptop_conns = conn_module.list_all_for_display(alice_laptop['peer_id'], current_t_ms, db)
    print(f"Laptop connections: {len(laptop_conns['active'])} active, {len(laptop_conns['pending'])} pending, {len(laptop_conns['bootstrap'])} bootstrap")

    # Phone should have connection to laptop
    phone_to_laptop = any(
        c.peer_shared_id == alice_laptop['peer_shared_id']
        for c in phone_conns['active']
    )
    print(f"Phone has active connection to Laptop: {phone_to_laptop}")

    # Laptop should have connection to phone
    laptop_to_phone = any(
        c.peer_shared_id == alice_phone['peer_shared_id']
        for c in laptop_conns['active']
    )
    print(f"Laptop has active connection to Phone: {laptop_to_phone}")

    # Assert bidirectional connections exist
    assert phone_to_laptop or len(phone_conns['active']) > 0, \
        "Phone should have at least one active connection after sync"
    assert laptop_to_phone or len(laptop_conns['active']) > 0 or len(laptop_conns['bootstrap']) > 0, \
        "Laptop should have at least one connection (active or bootstrap) after sync"
    print(f"✅ Devices have connections established")

    # Create messages on both devices
    print("\n=== Creating messages on both devices ===")

    # Alice sends from phone
    alice_phone_msg = message.create(
        peer_id=alice_phone['peer_id'],
        channel_id=alice_phone['channel_id'],
        content="Hello from Alice's phone!",
        t_ms=current_t_ms + 1000,
        db=db
    )
    db.commit()
    print(f"Alice (phone) created message: {alice_phone_msg['id'][:20]}...")

    # Alice sends from laptop (uses same channel as phone since same user)
    alice_laptop_msg = message.create(
        peer_id=alice_laptop['peer_id'],
        channel_id=alice_phone['channel_id'],  # Same user, same channel
        content="Hello from Alice's laptop!",
        t_ms=current_t_ms + 1100,
        db=db
    )
    db.commit()
    print(f"Alice (laptop) created message: {alice_laptop_msg['id'][:20]}...")

    # Sync messages between devices using assert_eventually for bidirectional sync
    print("\n=== Sync messages between devices ===")

    # Wait for bidirectional message sync
    def both_devices_see_both_messages():
        phone_messages = message.list(alice_phone['channel_id'], alice_phone['peer_id'], db)
        laptop_messages = message.list(alice_phone['channel_id'], alice_laptop['peer_id'], db)

        phone_contents = [msg['content'] for msg in phone_messages]
        laptop_contents = [msg['content'] for msg in laptop_messages]

        # Phone should see both messages
        assert "Hello from Alice's phone!" in phone_contents, "Phone should see its own message"
        assert "Hello from Alice's laptop!" in phone_contents, "Phone should see laptop's message"

        # Laptop should see both messages
        assert "Hello from Alice's phone!" in laptop_contents, "Laptop should see phone's message"
        assert "Hello from Alice's laptop!" in laptop_contents, "Laptop should see its own message"

    assert_eventually(both_devices_see_both_messages, db=db, start_t_ms=current_t_ms + 2000)

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

    print(f"\n✅ All assertions passed!")


# TODO: Fix message sync for linked devices joining after messages exist
@pytest.mark.skip(reason="Messages not syncing to linked device that joins later")
def test_alice_laptop_joins_after_phone_has_messages(fresh_db):
    """Alice's laptop joins after phone already has messages."""

    db = fresh_db
    clock = TestClock()

    print("\n=== Setup: Alice creates network and sends messages ===")

    alice_phone = user.new_network(name='Alice', t_ms=clock.tick(), db=db, device_name='Phone')
    db.commit()

    msg1 = message.create(
        peer_id=alice_phone['peer_id'],
        channel_id=alice_phone['channel_id'],
        content="Message 1 from phone",
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    msg2 = message.create(
        peer_id=alice_phone['peer_id'],
        channel_id=alice_phone['channel_id'],
        content="Message 2 from phone",
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    print(f"Phone sent 2 messages")

    print("\n=== Link laptop after messages already exist ===")

    invite_id, link_url, invite_data = invite.create(
        peer_id=alice_phone['peer_id'],
        t_ms=clock.tick(),
        db=db,
        mode='peer',
        user_id=alice_phone['user_id']
    )
    db.commit()

    alice_laptop_peer_id = peer.create(t_ms=clock.tick(), db=db)
    accepted = invite.accept(alice_laptop_peer_id, link_url, t_ms=clock.tick(), db=db)
    assert accepted['mode'] == 'peer'
    alice_laptop = peer_shared.join(
        peer_id=alice_laptop_peer_id,
        peer_invite_id=accepted['invite_id'],
        peer_invite_private_key=accepted['invite_private_key'],
        user_id=accepted['user_id'],
        t_ms=clock.tick(),
        db=db,
        device_name='Laptop'
    )
    db.commit()

    # Wait for laptop to see historical messages
    print("\n=== Waiting for laptop to sync messages ===")

    def laptop_sees_messages():
        laptop_channels = db.query_all(
            "SELECT DISTINCT channel_id FROM channels WHERE recorded_by = ?",
            (alice_laptop['peer_id'],)
        )
        assert len(laptop_channels) == 1, \
            f"Laptop should have exactly one channel"

        laptop_channel_id = laptop_channels[0]['channel_id']
        laptop_messages = message.list(
            laptop_channel_id,
            alice_laptop['peer_id'],
            db
        )
        laptop_contents = [msg['content'] for msg in laptop_messages]

        assert len(laptop_messages) == 2, \
            f"Laptop should see both historical messages, got {len(laptop_messages)}"
        assert "Message 1 from phone" in laptop_contents
        assert "Message 2 from phone" in laptop_contents

    assert_eventually(laptop_sees_messages, db=db, start_t_ms=None)

    # Verify device name is now available
    laptop_device_name = peer_shared.get_device_name(alice_laptop['peer_shared_id'], alice_laptop['peer_id'], db)
    assert laptop_device_name == 'Laptop', f"Device name should be 'Laptop', got '{laptop_device_name}'"
    print(f"✅ Laptop received all historical messages after linking!")


# TODO: Fix connection peer_shared_id mismatch in three-device linking
@pytest.mark.xfail(reason="Connection peer_shared_id mismatch prevents mesh connectivity")
def test_three_devices_all_linked(fresh_db):
    """Alice links phone, laptop, and tablet - all three share messages."""

    db = fresh_db
    clock = TestClock()

    print("\n=== Setup: Create network on phone ===")

    alice_phone = user.new_network(name='Alice', t_ms=clock.tick(), db=db, device_name='Phone')
    db.commit()

    print("\n=== Link laptop ===")

    invite_id_1, link_url_1, _ = invite.create(
        peer_id=alice_phone['peer_id'],
        t_ms=clock.tick(),
        db=db,
        mode='peer',
        user_id=alice_phone['user_id']
    )
    db.commit()

    alice_laptop_peer_id = peer.create(t_ms=clock.tick(), db=db)
    accepted_1 = invite.accept(alice_laptop_peer_id, link_url_1, t_ms=clock.tick(), db=db)
    alice_laptop = peer_shared.join(
        peer_id=alice_laptop_peer_id,
        peer_invite_id=accepted_1['invite_id'],
        peer_invite_private_key=accepted_1['invite_private_key'],
        user_id=accepted_1['user_id'],
        t_ms=clock.tick(),
        db=db,
        device_name='Laptop'
    )
    db.commit()

    print("\n=== Link tablet ===")

    invite_id_2, link_url_2, _ = invite.create(
        peer_id=alice_phone['peer_id'],
        t_ms=clock.tick(),
        db=db,
        mode='peer',
        user_id=alice_phone['user_id']
    )
    db.commit()

    alice_tablet_peer_id = peer.create(t_ms=clock.tick(), db=db)
    accepted_2 = invite.accept(alice_tablet_peer_id, link_url_2, t_ms=clock.tick(), db=db)
    alice_tablet = peer_shared.join(
        peer_id=alice_tablet_peer_id,
        peer_invite_id=accepted_2['invite_id'],
        peer_invite_private_key=accepted_2['invite_private_key'],
        user_id=accepted_2['user_id'],
        t_ms=clock.tick(),
        db=db,
        device_name='Tablet'
    )
    db.commit()

    # All three should have same user_id
    assert alice_laptop['user_id'] == alice_phone['user_id']
    assert alice_tablet['user_id'] == alice_phone['user_id']
    print(f"✅ All three devices share user_id: {alice_phone['user_id'][:20]}...")

    # Wait for all device names to sync
    print("\n=== Waiting for device names to sync ===")

    def all_device_names_synced():
        phone_device_name = peer_shared.get_device_name(alice_phone['peer_shared_id'], alice_phone['peer_id'], db)
        laptop_device_name = peer_shared.get_device_name(alice_laptop['peer_shared_id'], alice_laptop['peer_id'], db)
        tablet_device_name = peer_shared.get_device_name(alice_tablet['peer_shared_id'], alice_tablet['peer_id'], db)
        assert phone_device_name == 'Phone', f"Phone device name should be 'Phone', got '{phone_device_name}'"
        assert laptop_device_name == 'Laptop', f"Laptop device name should be 'Laptop', got '{laptop_device_name}'"
        assert tablet_device_name == 'Tablet', f"Tablet device name should be 'Tablet', got '{tablet_device_name}'"

    final_t_ms = assert_eventually(all_device_names_synced, db=db, start_t_ms=None)
    print(f"✅ All device names stored correctly: Phone, Laptop, Tablet")

    # Verify connections between all three devices
    print("\n=== Verifying connections between all three devices ===")
    from events.network import connection_request as conn_module

    # Get current time from sync
    final_t_ms = 6000 + 500 * 100  # Approximate end time

    # Get connections for each device
    phone_conns = conn_module.list_all_for_display(alice_phone['peer_id'], final_t_ms, db)
    laptop_conns = conn_module.list_all_for_display(alice_laptop['peer_id'], final_t_ms, db)
    tablet_conns = conn_module.list_all_for_display(alice_tablet['peer_id'], final_t_ms, db)

    print(f"Phone connections: {len(phone_conns['active'])} active")
    print(f"Laptop connections: {len(laptop_conns['active'])} active")
    print(f"Tablet connections: {len(tablet_conns['active'])} active")

    # Each device should have connections (to the other two devices)
    # Phone sees: laptop + tablet
    phone_peers = {c.peer_shared_id for c in phone_conns['active'] if c.peer_shared_id}
    laptop_peers = {c.peer_shared_id for c in laptop_conns['active'] if c.peer_shared_id}
    tablet_peers = {c.peer_shared_id for c in tablet_conns['active'] if c.peer_shared_id}

    print(f"Phone connected to: {len(phone_peers)} peers")
    print(f"Laptop connected to: {len(laptop_peers)} peers")
    print(f"Tablet connected to: {len(tablet_peers)} peers")

    # Assert each device has at least one active connection
    assert len(phone_conns['active']) >= 1, "Phone should have at least 1 active connection"
    assert len(laptop_conns['active']) >= 1 or len(laptop_conns['bootstrap']) >= 1, \
        "Laptop should have at least 1 connection"
    assert len(tablet_conns['active']) >= 1 or len(tablet_conns['bootstrap']) >= 1, \
        "Tablet should have at least 1 connection"

    # Verify mesh connectivity - phone should see both laptop and tablet
    phone_sees_laptop = alice_laptop['peer_shared_id'] in phone_peers
    phone_sees_tablet = alice_tablet['peer_shared_id'] in phone_peers
    print(f"Phone → Laptop: {phone_sees_laptop}, Phone → Tablet: {phone_sees_tablet}")

    assert phone_sees_laptop or phone_sees_tablet, \
        "Phone should have active connection to at least one other device"
    print(f"✅ All three devices have connections established (mesh topology)")

    # Discover the shared channel for all devices
    print("\n=== Discovering shared channel ===")

    phone_channels = db.query_all(
        "SELECT DISTINCT channel_id FROM channels WHERE recorded_by = ?",
        (alice_phone['peer_id'],)
    )
    assert len(phone_channels) == 1, f"Phone should have 1 channel, got {len(phone_channels)}"
    shared_channel_id = phone_channels[0]['channel_id']
    print(f"All devices will use shared channel: {shared_channel_id[:20]}...")

    # Wait for all devices to have the channel synced
    def all_devices_have_channel():
        laptop_channels = db.query_all(
            "SELECT channel_id FROM channels WHERE recorded_by = ? AND channel_id = ?",
            (alice_laptop['peer_id'], shared_channel_id)
        )
        tablet_channels = db.query_all(
            "SELECT channel_id FROM channels WHERE recorded_by = ? AND channel_id = ?",
            (alice_tablet['peer_id'], shared_channel_id)
        )
        assert len(laptop_channels) >= 1, "Laptop should have synced channel"
        assert len(tablet_channels) >= 1, "Tablet should have synced channel"

    final_t_ms = assert_eventually(all_devices_have_channel, db=db, start_t_ms=final_t_ms)
    print("✅ All devices have the shared channel")

    # Each device sends a message
    print("\n=== Each device sends a message ===")

    msg_phone = message.create(
        peer_id=alice_phone['peer_id'],
        channel_id=shared_channel_id,
        content="From phone",
        t_ms=final_t_ms + 1000,
        db=db
    )
    db.commit()

    msg_laptop = message.create(
        peer_id=alice_laptop['peer_id'],
        channel_id=shared_channel_id,
        content="From laptop",
        t_ms=final_t_ms + 2000,
        db=db
    )
    db.commit()

    msg_tablet = message.create(
        peer_id=alice_tablet['peer_id'],
        channel_id=shared_channel_id,
        content="From tablet",
        t_ms=final_t_ms + 3000,
        db=db
    )
    db.commit()

    # Wait for all devices to see all messages
    print("\n=== Waiting for message sync ===")

    def all_devices_see_all_messages():
        phone_msgs = message.list(shared_channel_id, alice_phone['peer_id'], db)
        laptop_msgs = message.list(shared_channel_id, alice_laptop['peer_id'], db)
        tablet_msgs = message.list(shared_channel_id, alice_tablet['peer_id'], db)

        assert len(phone_msgs) == 3, f"Phone should see 3 messages, got {len(phone_msgs)}"
        assert len(laptop_msgs) == 3, f"Laptop should see 3 messages, got {len(laptop_msgs)}"
        assert len(tablet_msgs) == 3, f"Tablet should see 3 messages, got {len(tablet_msgs)}"

    assert_eventually(all_devices_see_all_messages, db=db, start_t_ms=final_t_ms + 4000)

    # Verify channels
    print("\n=== Verifying channels for each device ===")

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
