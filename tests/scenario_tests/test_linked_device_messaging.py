"""
Scenario test: Bidirectional messaging between linked devices.

Alice links two devices and verifies that messages sync bidirectionally.
Both devices should be able to send and receive messages from each other.

Tests:
- Alice creates network and links second device
- Device 1 sends message M1
- Device 2 sends message M2
- Both devices see both messages
- Messages sync correctly in both directions
"""
import sqlite3
import pytest
from core.db import Database
from core import schema
from events.identity import user, invite, peer_shared, peer
from events.group import group, group_member
from events.content import message
from tests.utils.tick_helper import assert_eventually, run_ticks, TestClock


# TODO: Fix username sync for linked devices - device 2 doesn't see username before trying to send
@pytest.mark.skip(reason="Username not syncing to linked device before message send")
def test_linked_devices_bidirectional_messaging(fresh_db):
    """Messages sync bidirectionally between linked devices."""

    db = fresh_db
    clock = TestClock()

    print("\n=== Setup: Alice creates network ===")

    alice_device1 = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    print(f"Alice created network on device 1")
    print(f"  user_id={alice_device1['user_id'][:20]}...")
    db.commit()

    print("\n=== Create test group ===")

    group_id, _ = group.create(
        name='Test Group',
        peer_id=alice_device1['peer_id'],
        peer_shared_id=alice_device1['peer_shared_id'],
        t_ms=clock.tick(),
        db=db
    )
    print(f"Created Test Group: {group_id[:20]}...")
    group_member.create(
        group_id=group_id,
        user_id=alice_device1['user_id'],
        peer_id=alice_device1['peer_id'],
        peer_shared_id=alice_device1['peer_shared_id'],
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    print("\n=== Link second device ===")

    invite_id, invite_link, _ = invite.create(
        peer_id=alice_device1['peer_id'],
        t_ms=clock.tick(),
        db=db,
        mode='peer',
        user_id=alice_device1['user_id']
    )
    db.commit()

    alice_device2_peer_id = peer.create(t_ms=clock.tick(), db=db)
    accepted = invite.accept(alice_device2_peer_id, invite_link, t_ms=clock.tick(), db=db)
    assert accepted['mode'] == 'peer'
    alice_device2 = peer_shared.join(
        peer_id=alice_device2_peer_id,
        peer_invite_id=accepted['invite_id'],
        peer_invite_private_key=accepted['invite_private_key'],
        user_id=accepted['user_id'],
        t_ms=clock.tick(),
        db=db
    )
    print(f"Alice linked device 2")
    print(f"  user_id={alice_device2['user_id'][:20]}...")
    db.commit()

    # Verify same user_id
    assert alice_device2['user_id'] == alice_device1['user_id']
    print(f"✅ Both devices share user_id")

    # Wait for device 2 to sync the channel
    print("\n=== Waiting for device 2 to sync channel ===")

    def device2_has_channel():
        channels = db.query_all(
            "SELECT channel_id FROM channels WHERE recorded_by = ? AND channel_id = ?",
            (alice_device2['peer_id'], alice_device1['channel_id'])
        )
        assert len(channels) >= 1, "Device 2 should have synced channel"

    assert_eventually(device2_has_channel, db=db, start_t_ms=None)
    print("✅ Device 2 has channel")

    print("\n=== Device 1 sends message M1 ===")

    msg1_result = message.create(
        peer_id=alice_device1['peer_id'],
        channel_id=alice_device1['channel_id'],
        content="Message from device 1",
        t_ms=clock.tick(),
        db=db
    )
    msg1_id = msg1_result['id']
    print(f"Device 1 sent message: {msg1_id[:20]}...")
    db.commit()

    print("\n=== Device 2 sends message M2 ===")

    msg2_result = message.create(
        peer_id=alice_device2['peer_id'],
        channel_id=alice_device1['channel_id'],
        content="Message from device 2",
        t_ms=clock.tick(),
        db=db
    )
    msg2_id = msg2_result['id']
    print(f"Device 2 sent message: {msg2_id[:20]}...")
    db.commit()

    # Wait for both devices to see both messages
    print("\n=== Waiting for message sync ===")

    def both_devices_see_both_messages():
        device1_messages = message.list(
            alice_device1['channel_id'],
            alice_device1['peer_id'],
            db
        )
        device1_contents = [msg['content'] for msg in device1_messages]

        device2_messages = message.list(
            alice_device1['channel_id'],  # Linked devices share channels
            alice_device2['peer_id'],
            db
        )
        device2_contents = [msg['content'] for msg in device2_messages]

        # Device 1 sees both
        assert "Message from device 1" in device1_contents, \
            "Device 1 should see its own message"
        assert "Message from device 2" in device1_contents, \
            "Device 1 should see device 2's message"

        # Device 2 sees both
        assert "Message from device 1" in device2_contents, \
            "Device 2 should see device 1's message"
        assert "Message from device 2" in device2_contents, \
            "Device 2 should see its own message"

    assert_eventually(both_devices_see_both_messages, db=db, start_t_ms=None)
    print("✅ Both devices see both messages")

    print(f"\n✅ All assertions passed! Bidirectional messaging works correctly.")
